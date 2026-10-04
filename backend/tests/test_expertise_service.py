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
from app.services.expertise_service import repository_expertise, suggest_reviewers
from app.services.merge_service import MergeActor, merge_service


class FakeReviewerRationaleProvider(LLMProvider):
    provider_name = "OPENROUTER"

    def __init__(self, answer: str = "The top candidate has touched most of the changed sheet recently."):
        self.answer = answer
        self.calls = 0

    async def complete(self, **kwargs) -> ProviderResult:
        self.calls += 1
        content = json.dumps({
            "answer": self.answer, "evidence": [], "confidence": 0.75,
            "insufficient_evidence": False, "recommended_actions": [], "warnings": [],
        })
        return ProviderResult(content, input_tokens=40, output_tokens=20, reasoning_tokens=0)


class FailingProvider(LLMProvider):
    provider_name = "OPENROUTER"

    async def complete(self, **kwargs):
        raise RuntimeError("provider unavailable")


class ExpertiseServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "expertise.db"
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute(
            'CREATE TABLE "QUEUE_BOARD_EXPERTISE" (ROW_ID INTEGER PRIMARY KEY, "TEAM" TEXT, "SCORE" INTEGER)'
        )
        conn.executemany(
            'INSERT INTO "QUEUE_BOARD_EXPERTISE" (ROW_ID, "TEAM", "SCORE") VALUES (?, ?, ?)',
            [(1, "KKR", 10), (2, "CSK", 20)],
        )
        conn.commit()
        conn.close()
        database.initialize_product_schema()

        self.owner = database.get_or_create_user("owner-expertise@example.com")
        self.expert = database.get_or_create_user("expert-expertise@example.com")
        self.bystander = database.get_or_create_user("bystander-expertise@example.com")

        registered = database.register_dataset(
            "QUEUE_BOARD_EXPERTISE", self.owner["user_id"], "expertise.xlsx", 2, 2
        )
        database.add_dataset_member("QUEUE_BOARD_EXPERTISE", self.owner["user_id"], self.expert["email"], "editor")
        database.add_dataset_member("QUEUE_BOARD_EXPERTISE", self.owner["user_id"], self.bystander["email"], "editor")
        database.save_user_ai_settings(self.owner["user_id"], encrypt_secret("sk-or-test-expertise"), settings.openrouter_model)

        self.repository_id = registered["repository_id"]
        self.table_id = "QUEUE_BOARD_EXPERTISE"
        conn = database._get_connection()
        self.organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (self.repository_id,)
        ).fetchone()[0]
        conn.close()

        self.owner_actor = MergeActor(self.owner["user_id"], self.owner["email"])
        self.expert_actor = MergeActor(self.expert["user_id"], self.expert["email"])

        self._original_provider = ai_gateway.providers.get("OPENROUTER")

    def tearDown(self):
        if self._original_provider is not None:
            ai_gateway.providers["OPENROUTER"] = self._original_provider
        else:
            ai_gateway.providers.pop("OPENROUTER", None)
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _commit_cell(self, branch_id, table_id, column_name, value, actor, sheet_index=0):
        snapshot = database.get_table_snapshot(table_id)
        sheet = snapshot["semantic"]["sheets"][sheet_index]
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

    def _sheet_id(self):
        snapshot = database.get_table_snapshot(self.table_id)
        return snapshot["semantic"]["sheets"][0]["sheet_id"]

    def _backdate_commit(self, commit_id, days_ago):
        when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        conn = database._get_connection()
        try:
            conn.execute("UPDATE COMMITS SET CREATED_AT=? WHERE COMMIT_ID=?", (when, commit_id))
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

    # ------------------------------------------------------- expertise --
    def test_repository_expertise_attributes_touches_to_the_real_author(self):
        expert_copy = database.create_working_copy(self.table_id, self.expert["user_id"], self.expert["email"])
        # The expert commits three times on this sheet — far more signal
        # than anyone else will have.
        for value in ("MI", "RCB", "DC"):
            self._commit_cell(expert_copy["branch_id"], expert_copy["table_id"], "TEAM", value, self.expert_actor)

        result = repository_expertise(self.repository_id)
        by_user = {row["user_id"]: row for row in result["experts"]}
        self.assertIn(self.expert["user_id"], by_user)
        expert_row = by_user[self.expert["user_id"]]
        self.assertEqual(expert_row["commits"], 3)
        self.assertEqual(expert_row["top_sheets"][0]["touches"], 3)
        self.assertEqual(expert_row["top_sheets"][0]["sheet_id"], self._sheet_id())

    def test_repository_expertise_counts_open_merge_requests_as_workload(self):
        expert_copy = database.create_working_copy(self.table_id, self.expert["user_id"], self.expert["email"])
        self._commit_cell(expert_copy["branch_id"], expert_copy["table_id"], "TEAM", "MI", self.expert_actor)
        merge_service.create_request(
            source_branch_id=expert_copy["branch_id"], target_branch_id=database.get_repository(self.table_id, self.owner["user_id"])["default_branch_id"],
            title="Expert's own change", description="", actor=self.expert_actor,
        )

        result = repository_expertise(self.repository_id)
        expert_row = next(row for row in result["experts"] if row["user_id"] == self.expert["user_id"])
        self.assertEqual(expert_row["open_merge_requests"], 1)

    def test_repository_expertise_decays_old_touches_below_recent_ones(self):
        stale_copy = database.create_working_copy(self.table_id, self.expert["user_id"], self.expert["email"])
        stale_commit = self._commit_cell(stale_copy["branch_id"], stale_copy["table_id"], "TEAM", "MI", self.expert_actor)
        self._backdate_commit(stale_commit["commit_id"], days_ago=90)

        fresh_copy = database.create_working_copy(self.table_id, self.bystander["user_id"], self.bystander["email"])
        bystander_actor = MergeActor(self.bystander["user_id"], self.bystander["email"])
        self._commit_cell(fresh_copy["branch_id"], fresh_copy["table_id"], "SCORE", 7, bystander_actor)

        result = repository_expertise(self.repository_id, days=180)
        by_user = {row["user_id"]: row for row in result["experts"]}
        stale_sheet = by_user[self.expert["user_id"]]["top_sheets"][0]
        fresh_sheet = by_user[self.bystander["user_id"]]["top_sheets"][0]

        self.assertEqual(stale_sheet["touches"], fresh_sheet["touches"], "both made exactly one touch")
        self.assertLess(
            stale_sheet["score"], fresh_sheet["score"],
            "a 90-day-old touch must score lower than an equally-sized touch made today",
        )

    def test_repository_expertise_open_workload_days_grows_with_staleness(self):
        expert_copy = database.create_working_copy(self.table_id, self.expert["user_id"], self.expert["email"])
        self._commit_cell(expert_copy["branch_id"], expert_copy["table_id"], "TEAM", "MI", self.expert_actor)
        stale_request = merge_service.create_request(
            source_branch_id=expert_copy["branch_id"],
            target_branch_id=database.get_repository(self.table_id, self.owner["user_id"])["default_branch_id"],
            title="A stale request", description="", actor=self.expert_actor,
        )
        self._backdate_merge_request(stale_request["merge_request_id"], days_ago=14)

        result = repository_expertise(self.repository_id)
        expert_row = next(row for row in result["experts"] if row["user_id"] == self.expert["user_id"])
        self.assertEqual(expert_row["open_merge_requests"], 1)
        self.assertGreaterEqual(expert_row["open_workload_days"], 14.0, "a two-week-old open request should weigh at least 14 workload-days")

    # ------------------------------------------------- suggest_reviewers --
    async def test_suggest_reviewers_ranks_real_expertise_first_and_excludes_the_author(self):
        expert_copy = database.create_working_copy(self.table_id, self.expert["user_id"], self.expert["email"])
        for value in ("MI", "RCB"):
            self._commit_cell(expert_copy["branch_id"], expert_copy["table_id"], "TEAM", value, self.expert_actor)
        merge_service.merge(
            merge_service.review(
                merge_request_id=merge_service.create_request(
                    source_branch_id=expert_copy["branch_id"],
                    target_branch_id=database.get_repository(self.table_id, self.owner["user_id"])["default_branch_id"],
                    title="Land expert work", description="", actor=self.expert_actor,
                )["merge_request_id"],
                decision="APPROVED", comment="ok", actor=self.owner_actor,
            )["merge_request_id"],
            self.owner_actor,
        )

        owner_copy = database.create_working_copy(self.table_id, self.owner["user_id"], self.owner["email"])
        self._commit_cell(owner_copy["branch_id"], owner_copy["table_id"], "TEAM", "SRH", self.owner_actor)
        request = merge_service.get_request(
            merge_service.create_request(
                source_branch_id=owner_copy["branch_id"],
                target_branch_id=database.get_repository(self.table_id, self.owner["user_id"])["default_branch_id"],
                title="Owner's change", description="", actor=self.owner_actor,
            )["merge_request_id"],
            self.owner_actor,
        )

        result = await suggest_reviewers(self.organization_id, self.owner["user_id"], request, use_ai=False)

        candidate_ids = [c["user_id"] for c in result["candidates"]]
        self.assertNotIn(self.owner["user_id"], candidate_ids, "a merge request's own author must never be suggested as its reviewer")
        self.assertEqual(candidate_ids[0], self.expert["user_id"], "the person with real, relevant edit history on the touched sheet should rank first")
        self.assertGreater(result["candidates"][0]["relevant_touches"], 0)
        self.assertIsNone(result["rationale"])

    async def test_suggest_reviewers_breaks_ties_by_lighter_open_workload(self):
        # Two candidates with identical expertise on the touched sheet;
        # only one of them has an open merge request of their own.
        busy_copy = database.create_working_copy(self.table_id, self.expert["user_id"], self.expert["email"])
        self._commit_cell(busy_copy["branch_id"], busy_copy["table_id"], "TEAM", "MI", self.expert_actor)
        self._commit_cell(busy_copy["branch_id"], busy_copy["table_id"], "SCORE", 42, self.expert_actor)
        merge_service.create_request(
            source_branch_id=busy_copy["branch_id"],
            target_branch_id=database.get_repository(self.table_id, self.owner["user_id"])["default_branch_id"],
            title="Busy candidate's own open work", description="", actor=self.expert_actor,
        )

        bystander_actor = MergeActor(self.bystander["user_id"], self.bystander["email"])
        free_copy = database.create_working_copy(self.table_id, self.bystander["user_id"], self.bystander["email"])
        self._commit_cell(free_copy["branch_id"], free_copy["table_id"], "SCORE", 99, bystander_actor)
        merge_service.merge(
            merge_service.review(
                merge_request_id=merge_service.create_request(
                    source_branch_id=free_copy["branch_id"],
                    target_branch_id=database.get_repository(self.table_id, self.owner["user_id"])["default_branch_id"],
                    title="Free candidate lands work", description="", actor=bystander_actor,
                )["merge_request_id"],
                decision="APPROVED", comment="ok", actor=self.owner_actor,
            )["merge_request_id"],
            self.owner_actor,
        )
        # Give the free candidate the SAME relevant-sheet expertise as the busy one.
        second_copy = database.create_working_copy(self.table_id, self.bystander["user_id"], self.bystander["email"])
        self._commit_cell(second_copy["branch_id"], second_copy["table_id"], "TEAM", "CSK", bystander_actor)
        merge_service.merge(
            merge_service.review(
                merge_request_id=merge_service.create_request(
                    source_branch_id=second_copy["branch_id"],
                    target_branch_id=database.get_repository(self.table_id, self.owner["user_id"])["default_branch_id"],
                    title="Free candidate lands more work", description="", actor=bystander_actor,
                )["merge_request_id"],
                decision="APPROVED", comment="ok", actor=self.owner_actor,
            )["merge_request_id"],
            self.owner_actor,
        )

        owner_copy = database.create_working_copy(self.table_id, self.owner["user_id"], self.owner["email"])
        self._commit_cell(owner_copy["branch_id"], owner_copy["table_id"], "TEAM", "SRH", self.owner_actor)
        request = merge_service.get_request(
            merge_service.create_request(
                source_branch_id=owner_copy["branch_id"],
                target_branch_id=database.get_repository(self.table_id, self.owner["user_id"])["default_branch_id"],
                title="Owner's change", description="", actor=self.owner_actor,
            )["merge_request_id"],
            self.owner_actor,
        )

        result = await suggest_reviewers(self.organization_id, self.owner["user_id"], request, use_ai=False)
        candidates = {c["user_id"]: c for c in result["candidates"]}
        self.assertEqual(candidates[self.expert["user_id"]]["relevant_touches"], candidates[self.bystander["user_id"]]["relevant_touches"])
        ranked_ids = [c["user_id"] for c in result["candidates"]]
        self.assertLess(ranked_ids.index(self.bystander["user_id"]), ranked_ids.index(self.expert["user_id"]),
                         "with equal expertise, whoever has fewer open merge requests should rank first")

    async def test_suggest_reviewers_prefers_whoever_has_fewer_stale_days_of_open_work(self):
        # Both candidates end up with exactly ONE open merge request each —
        # equal by the old raw-count metric — but one of those requests has
        # been sitting for two weeks. The staler one should rank worse.
        stale_copy = database.create_working_copy(self.table_id, self.expert["user_id"], self.expert["email"])
        self._commit_cell(stale_copy["branch_id"], stale_copy["table_id"], "TEAM", "MI", self.expert_actor)
        stale_request = merge_service.create_request(
            source_branch_id=stale_copy["branch_id"],
            target_branch_id=database.get_repository(self.table_id, self.owner["user_id"])["default_branch_id"],
            title="Stale open work", description="", actor=self.expert_actor,
        )
        self._backdate_merge_request(stale_request["merge_request_id"], days_ago=14)

        bystander_actor = MergeActor(self.bystander["user_id"], self.bystander["email"])
        fresh_copy = database.create_working_copy(self.table_id, self.bystander["user_id"], self.bystander["email"])
        self._commit_cell(fresh_copy["branch_id"], fresh_copy["table_id"], "TEAM", "CSK", bystander_actor)
        merge_service.create_request(
            source_branch_id=fresh_copy["branch_id"],
            target_branch_id=database.get_repository(self.table_id, self.owner["user_id"])["default_branch_id"],
            title="Fresh open work", description="", actor=bystander_actor,
        )

        owner_copy = database.create_working_copy(self.table_id, self.owner["user_id"], self.owner["email"])
        self._commit_cell(owner_copy["branch_id"], owner_copy["table_id"], "TEAM", "SRH", self.owner_actor)
        request = merge_service.get_request(
            merge_service.create_request(
                source_branch_id=owner_copy["branch_id"],
                target_branch_id=database.get_repository(self.table_id, self.owner["user_id"])["default_branch_id"],
                title="Owner's change", description="", actor=self.owner_actor,
            )["merge_request_id"],
            self.owner_actor,
        )

        result = await suggest_reviewers(self.organization_id, self.owner["user_id"], request, use_ai=False)
        candidates = {c["user_id"]: c for c in result["candidates"]}
        self.assertEqual(candidates[self.expert["user_id"]]["open_merge_requests"], 1)
        self.assertEqual(candidates[self.bystander["user_id"]]["open_merge_requests"], 1)
        self.assertGreater(candidates[self.expert["user_id"]]["open_workload_days"], candidates[self.bystander["user_id"]]["open_workload_days"])
        ranked_ids = [c["user_id"] for c in result["candidates"]]
        self.assertLess(
            ranked_ids.index(self.bystander["user_id"]), ranked_ids.index(self.expert["user_id"]),
            "the candidate whose open request is two weeks stale should rank behind the one whose request is brand new",
        )

    async def test_suggest_reviewers_falls_back_to_full_roster_when_nobody_has_touched_the_sheet_yet(self):
        # A brand-new repository state: the owner's own branch is the only
        # commit history, so no OTHER member has any relevant touches.
        owner_copy = database.create_working_copy(self.table_id, self.owner["user_id"], self.owner["email"])
        self._commit_cell(owner_copy["branch_id"], owner_copy["table_id"], "TEAM", "SRH", self.owner_actor)
        request = merge_service.get_request(
            merge_service.create_request(
                source_branch_id=owner_copy["branch_id"],
                target_branch_id=database.get_repository(self.table_id, self.owner["user_id"])["default_branch_id"],
                title="Owner's change", description="", actor=self.owner_actor,
            )["merge_request_id"],
            self.owner_actor,
        )

        result = await suggest_reviewers(self.organization_id, self.owner["user_id"], request, use_ai=False)

        self.assertGreater(len(result["candidates"]), 0, "a repository with other members should always suggest someone, even with zero expertise signal")
        self.assertNotIn(self.owner["user_id"], [c["user_id"] for c in result["candidates"]])

    async def test_ai_rationale_explains_the_top_candidate_without_changing_the_ranking(self):
        ai_gateway.providers["OPENROUTER"] = FakeReviewerRationaleProvider()
        expert_copy = database.create_working_copy(self.table_id, self.expert["user_id"], self.expert["email"])
        self._commit_cell(expert_copy["branch_id"], expert_copy["table_id"], "TEAM", "MI", self.expert_actor)
        merge_service.merge(
            merge_service.review(
                merge_request_id=merge_service.create_request(
                    source_branch_id=expert_copy["branch_id"],
                    target_branch_id=database.get_repository(self.table_id, self.owner["user_id"])["default_branch_id"],
                    title="Land expert work", description="", actor=self.expert_actor,
                )["merge_request_id"],
                decision="APPROVED", comment="ok", actor=self.owner_actor,
            )["merge_request_id"],
            self.owner_actor,
        )
        owner_copy = database.create_working_copy(self.table_id, self.owner["user_id"], self.owner["email"])
        self._commit_cell(owner_copy["branch_id"], owner_copy["table_id"], "TEAM", "SRH", self.owner_actor)
        request = merge_service.get_request(
            merge_service.create_request(
                source_branch_id=owner_copy["branch_id"],
                target_branch_id=database.get_repository(self.table_id, self.owner["user_id"])["default_branch_id"],
                title="Owner's change", description="", actor=self.owner_actor,
            )["merge_request_id"],
            self.owner_actor,
        )

        result = await suggest_reviewers(self.organization_id, self.owner["user_id"], request, use_ai=True)

        self.assertIsNotNone(result["rationale"])
        self.assertEqual(result["candidates"][0]["user_id"], self.expert["user_id"])

    async def test_ai_failure_never_breaks_the_deterministic_ranking(self):
        ai_gateway.providers["OPENROUTER"] = FailingProvider()
        expert_copy = database.create_working_copy(self.table_id, self.expert["user_id"], self.expert["email"])
        self._commit_cell(expert_copy["branch_id"], expert_copy["table_id"], "TEAM", "MI", self.expert_actor)
        merge_service.merge(
            merge_service.review(
                merge_request_id=merge_service.create_request(
                    source_branch_id=expert_copy["branch_id"],
                    target_branch_id=database.get_repository(self.table_id, self.owner["user_id"])["default_branch_id"],
                    title="Land expert work", description="", actor=self.expert_actor,
                )["merge_request_id"],
                decision="APPROVED", comment="ok", actor=self.owner_actor,
            )["merge_request_id"],
            self.owner_actor,
        )
        owner_copy = database.create_working_copy(self.table_id, self.owner["user_id"], self.owner["email"])
        self._commit_cell(owner_copy["branch_id"], owner_copy["table_id"], "TEAM", "SRH", self.owner_actor)
        request = merge_service.get_request(
            merge_service.create_request(
                source_branch_id=owner_copy["branch_id"],
                target_branch_id=database.get_repository(self.table_id, self.owner["user_id"])["default_branch_id"],
                title="Owner's change", description="", actor=self.owner_actor,
            )["merge_request_id"],
            self.owner_actor,
        )

        result = await suggest_reviewers(self.organization_id, self.owner["user_id"], request, use_ai=True)

        self.assertIsNone(result["rationale"])
        self.assertGreater(len(result["candidates"]), 0)
        self.assertEqual(result["candidates"][0]["user_id"], self.expert["user_id"])


if __name__ == "__main__":
    unittest.main()
