import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import database
from app.ai.gateway import ai_gateway
from app.ai.provider import LLMProvider, ProviderResult
from app.config import settings
from app.repositories.commit_store import commit_semantic_delta
from app.repositories.merge_store import branch_context
from app.secret_store import encrypt_secret
from app.services.merge_service import MergeActor, merge_service
from app.services.team_signals import (
    check_team_signals,
    detect_behavioral_anomalies,
    onboarding_ramp,
    team_health_digest,
)


class FakeDigestProvider(LLMProvider):
    provider_name = "OPENROUTER"

    def __init__(self, answer: str = "The team looks healthy overall, with one workload concern to watch."):
        self.answer = answer
        self.calls = 0

    async def complete(self, **kwargs) -> ProviderResult:
        self.calls += 1
        content = json.dumps({
            "answer": self.answer, "evidence": [], "confidence": 0.7,
            "insufficient_evidence": False, "recommended_actions": [], "warnings": [],
        })
        return ProviderResult(content, input_tokens=30, output_tokens=15, reasoning_tokens=0)


class FailingProvider(LLMProvider):
    provider_name = "OPENROUTER"

    async def complete(self, **kwargs):
        raise RuntimeError("provider unavailable")


class TeamSignalsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "team_signals.db"
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute(
            'CREATE TABLE "QUEUE_BOARD_SIGNALS" (ROW_ID INTEGER PRIMARY KEY, "TEAM" TEXT, "SCORE" INTEGER)'
        )
        conn.executemany(
            'INSERT INTO "QUEUE_BOARD_SIGNALS" (ROW_ID, "TEAM", "SCORE") VALUES (?, ?, ?)',
            [(1, "KKR", 10), (2, "CSK", 20)],
        )
        conn.commit()
        conn.close()
        database.initialize_product_schema()

        self.owner = database.get_or_create_user("owner-signals@example.com")
        self.member = database.get_or_create_user("member-signals@example.com")
        registered = database.register_dataset("QUEUE_BOARD_SIGNALS", self.owner["user_id"], "signals.xlsx", 2, 2)
        database.add_dataset_member("QUEUE_BOARD_SIGNALS", self.owner["user_id"], self.member["email"], "editor")
        database.save_user_ai_settings(self.owner["user_id"], encrypt_secret("sk-or-test-signals"), settings.openrouter_model)

        self.repository_id = registered["repository_id"]
        self.table_id = "QUEUE_BOARD_SIGNALS"
        conn = database._get_connection()
        self.organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (self.repository_id,)
        ).fetchone()[0]
        conn.close()
        self.member_actor = MergeActor(self.member["user_id"], self.member["email"])
        self._original_provider = ai_gateway.providers.get("OPENROUTER")

    def tearDown(self):
        if self._original_provider is not None:
            ai_gateway.providers["OPENROUTER"] = self._original_provider
        else:
            ai_gateway.providers.pop("OPENROUTER", None)
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _commit_cell(self, branch_id, table_id, column_name, value, actor):
        snapshot = database.get_table_snapshot(table_id)
        sheet = snapshot["semantic"]["sheets"][0]
        row = sheet["rows"][0]
        column = next(item for item in sheet["columns"] if item["name"] == column_name)
        context = branch_context(branch_id)
        return commit_semantic_delta(
            table_id=table_id, repository_id=context["repository_id"], branch_id=branch_id,
            expected_head_commit_id=snapshot["head_commit_id"], base_version=snapshot["version"],
            changes=[{"operation_type": "CELL_VALUE_UPDATE", "sheet_id": sheet["sheet_id"],
                      "row_id": row["row_id"], "column_id": column["column_id"], "new_value": value}],
            user_id=actor.user_id, user_email=actor.email, message=f"Set {column_name}",
        )

    def _backdate_commit(self, commit_id, days_ago):
        when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        conn = database._get_connection()
        try:
            conn.execute("UPDATE COMMITS SET CREATED_AT=? WHERE COMMIT_ID=?", (when, commit_id))
            conn.commit()
        finally:
            conn.close()

    def _backdate_membership(self, user_id, days_ago):
        when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        conn = database._get_connection()
        try:
            conn.execute(
                "UPDATE REPOSITORY_MEMBERS SET CREATED_AT=? WHERE REPOSITORY_ID=? AND USER_ID=?",
                (when, self.repository_id, user_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _backdate_merge_request(self, merge_request_id, days_ago):
        when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        conn = database._get_connection()
        try:
            conn.execute("UPDATE MERGE_REQUESTS SET CREATED_AT=? WHERE MERGE_REQUEST_ID=?", (when, merge_request_id))
            conn.commit()
        finally:
            conn.close()

    # --------------------------------------------------------- onboarding --
    def test_ramp_days_measures_time_from_joining_to_first_commit(self):
        self._backdate_membership(self.member["user_id"], days_ago=5)
        copy = database.create_working_copy(self.table_id, self.member["user_id"], self.member["email"])
        self._commit_cell(copy["branch_id"], copy["table_id"], "TEAM", "MI", self.member_actor)

        result = onboarding_ramp(self.repository_id)
        member_row = next(item for item in result["members"] if item["user_id"] == self.member["user_id"])
        self.assertIsNotNone(member_row["ramp_days"])
        self.assertAlmostEqual(member_row["ramp_days"], 5.0, delta=0.1)
        self.assertFalse(member_row["stalled"])
        self.assertIsNotNone(result["average_ramp_days"])

    def test_member_joined_over_a_week_ago_with_no_commits_is_stalled(self):
        self._backdate_membership(self.member["user_id"], days_ago=10)

        result = onboarding_ramp(self.repository_id)
        member_row = next(item for item in result["members"] if item["user_id"] == self.member["user_id"])
        self.assertTrue(member_row["stalled"])
        self.assertIsNone(member_row["ramp_days"])
        self.assertEqual(result["stalled_count"], 1)

    def test_member_joined_recently_with_no_commits_is_not_yet_stalled(self):
        # Default fixture: member joined "just now" via add_dataset_member.
        result = onboarding_ramp(self.repository_id)
        member_row = next(item for item in result["members"] if item["user_id"] == self.member["user_id"])
        self.assertFalse(member_row["stalled"])
        self.assertEqual(result["stalled_count"], 0)

    # ----------------------------------------------- behavioral anomalies --
    def test_a_genuine_volume_spike_against_own_baseline_is_flagged(self):
        for offset, value in [(6, "MI"), (5, "RCB"), (4, "CSK"), (3, "DC")]:
            copy = database.create_working_copy(self.table_id, self.member["user_id"], self.member["email"])
            commit = self._commit_cell(copy["branch_id"], copy["table_id"], "TEAM", value, self.member_actor)
            self._backdate_commit(commit["commit_id"], days_ago=offset)
        # A real spike: many more changes than any prior day, well past 3x.
        big_copy = database.create_working_copy(self.table_id, self.member["user_id"], self.member["email"])
        snapshot = database.get_table_snapshot(big_copy["table_id"])
        sheet = snapshot["semantic"]["sheets"][0]
        column = next(item for item in sheet["columns"] if item["name"] == "TEAM")
        changes = [
            {"operation_type": "CELL_VALUE_UPDATE", "sheet_id": sheet["sheet_id"], "row_id": row["row_id"],
             "column_id": column["column_id"], "new_value": f"V{i}"}
            for i, row in enumerate(sheet["rows"] * 15)
        ]
        commit_semantic_delta(
            table_id=big_copy["table_id"], repository_id=self.repository_id, branch_id=big_copy["branch_id"],
            expected_head_commit_id=snapshot["head_commit_id"], base_version=snapshot["version"],
            changes=changes, user_id=self.member["user_id"], user_email=self.member["email"], message="Big update",
        )

        result = detect_behavioral_anomalies(self.repository_id, baseline_days=30)
        self.assertTrue(any(item["user_id"] == self.member["user_id"] for item in result["anomalies"]))
        anomaly = next(item for item in result["anomalies"] if item["user_id"] == self.member["user_id"])
        self.assertGreaterEqual(anomaly["ratio"], 3.0)

    def test_ordinary_day_to_day_variance_is_never_flagged(self):
        for offset, count in [(4, 2), (3, 3), (2, 2), (1, 4)]:
            copy = database.create_working_copy(self.table_id, self.member["user_id"], self.member["email"])
            snapshot = database.get_table_snapshot(copy["table_id"])
            sheet = snapshot["semantic"]["sheets"][0]
            column = next(item for item in sheet["columns"] if item["name"] == "TEAM")
            changes = [
                {"operation_type": "CELL_VALUE_UPDATE", "sheet_id": sheet["sheet_id"], "row_id": sheet["rows"][0]["row_id"],
                 "column_id": column["column_id"], "new_value": f"V{i}"}
                for i in range(count)
            ]
            commit = commit_semantic_delta(
                table_id=copy["table_id"], repository_id=self.repository_id, branch_id=copy["branch_id"],
                expected_head_commit_id=snapshot["head_commit_id"], base_version=snapshot["version"],
                changes=changes, user_id=self.member["user_id"], user_email=self.member["email"], message="Small update",
            )
            self._backdate_commit(commit["commit_id"], days_ago=offset)

        result = detect_behavioral_anomalies(self.repository_id, baseline_days=30)
        self.assertFalse(any(item["user_id"] == self.member["user_id"] for item in result["anomalies"]))

    def test_insufficient_history_is_never_flagged_even_with_a_huge_first_day(self):
        copy = database.create_working_copy(self.table_id, self.member["user_id"], self.member["email"])
        snapshot = database.get_table_snapshot(copy["table_id"])
        sheet = snapshot["semantic"]["sheets"][0]
        column = next(item for item in sheet["columns"] if item["name"] == "TEAM")
        changes = [
            {"operation_type": "CELL_VALUE_UPDATE", "sheet_id": sheet["sheet_id"], "row_id": row["row_id"],
             "column_id": column["column_id"], "new_value": f"V{i}"}
            for i, row in enumerate(sheet["rows"] * 20)
        ]
        commit_semantic_delta(
            table_id=copy["table_id"], repository_id=self.repository_id, branch_id=copy["branch_id"],
            expected_head_commit_id=snapshot["head_commit_id"], base_version=snapshot["version"],
            changes=changes, user_id=self.member["user_id"], user_email=self.member["email"], message="First ever commit",
        )

        result = detect_behavioral_anomalies(self.repository_id, baseline_days=30)
        self.assertFalse(any(item["user_id"] == self.member["user_id"] for item in result["anomalies"]))

    # ----------------------------------------------------- health digest --
    async def test_digest_observations_reflect_a_stalled_member(self):
        self._backdate_membership(self.member["user_id"], days_ago=10)

        result = await team_health_digest(self.organization_id, self.owner["user_id"], self.repository_id, use_ai=False)
        self.assertEqual(result["stalled_onboarding"], 1)
        self.assertTrue(any("haven't made a first commit" in observation for observation in result["observations"]))
        self.assertIsNone(result["narrative"])

    async def test_digest_narrative_is_grounded_when_ai_succeeds(self):
        ai_gateway.providers["OPENROUTER"] = FakeDigestProvider()
        result = await team_health_digest(self.organization_id, self.owner["user_id"], self.repository_id, use_ai=True)
        self.assertIsNotNone(result["narrative"])

    async def test_digest_narrative_is_none_when_ai_fails_without_blocking_the_digest(self):
        self._backdate_membership(self.member["user_id"], days_ago=10)  # vary state so the response cache can't mask the failure
        ai_gateway.providers["OPENROUTER"] = FailingProvider()
        result_failed = await team_health_digest(self.organization_id, self.owner["user_id"], self.repository_id, use_ai=True)
        self.assertIsNone(result_failed["narrative"])
        self.assertEqual(result_failed["stalled_onboarding"], 1)

    # --------------------------------------------------- notifications --
    def test_review_queue_backlog_notifies_the_owner_and_is_not_duplicated_within_cooldown(self):
        copy = database.create_working_copy(self.table_id, self.member["user_id"], self.member["email"])
        self._commit_cell(copy["branch_id"], copy["table_id"], "TEAM", "MI", self.member_actor)
        request = merge_service.create_request(
            source_branch_id=copy["branch_id"],
            target_branch_id=database.get_repository(self.table_id, self.owner["user_id"])["default_branch_id"],
            title="Stale request", description="", actor=self.member_actor,
        )
        self._backdate_merge_request(request["merge_request_id"], days_ago=14)

        check_team_signals(self.repository_id, self.owner["user_id"], self.organization_id)
        notifications = database.list_notifications(self.owner["user_id"])
        backlog = [item for item in notifications if item["type"] == "REVIEW_QUEUE_BACKLOG"]
        self.assertEqual(1, len(backlog))

        check_team_signals(self.repository_id, self.owner["user_id"], self.organization_id)
        notifications_again = database.list_notifications(self.owner["user_id"])
        backlog_again = [item for item in notifications_again if item["type"] == "REVIEW_QUEUE_BACKLOG"]
        self.assertEqual(1, len(backlog_again), "the same backlog must not be re-notified within the cooldown window")

    def test_onboarding_stalled_notifies_the_owner(self):
        self._backdate_membership(self.member["user_id"], days_ago=10)

        check_team_signals(self.repository_id, self.owner["user_id"], self.organization_id)

        notifications = database.list_notifications(self.owner["user_id"])
        stalled = [item for item in notifications if item["type"] == "ONBOARDING_STALLED"]
        self.assertEqual(1, len(stalled))
        self.assertIn(self.member["user_id"], stalled[0]["resource_id"])

    def test_check_team_signals_never_raises_even_if_something_internal_fails(self):
        # A nonexistent repository id should make every internal query
        # return empty results rather than raising — and even if it did,
        # check_team_signals must swallow it.
        try:
            check_team_signals("REPO_DOES_NOT_EXIST", self.owner["user_id"], self.organization_id)
        except Exception as exc:  # pragma: no cover - this is exactly what must never happen
            self.fail(f"check_team_signals raised: {exc}")


if __name__ == "__main__":
    unittest.main()
