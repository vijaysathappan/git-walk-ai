import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import database
from app.ai.personal_activity import personal_activity


def _iso(offset_days: float, hour: int = 10) -> str:
    when = datetime.now(timezone.utc) - timedelta(days=offset_days)
    return when.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


class PersonalActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "personal_activity.db"
        database.initialize_product_schema()

        self.user = database.get_or_create_user("activity-owner@example.com")
        self.peer = database.get_or_create_user("activity-peer@example.com")

        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_ACTIVITY" (ROW_ID INTEGER PRIMARY KEY, "TEAM" TEXT)')
        conn.execute('INSERT INTO "QUEUE_BOARD_ACTIVITY" (ROW_ID, "TEAM") VALUES (1, ?)', ("KKR",))
        conn.commit()
        conn.close()
        registered = database.register_dataset("QUEUE_BOARD_ACTIVITY", self.user["user_id"], "activity.xlsx", 1, 1)

        conn = database._get_connection()
        try:
            self.organization_id = conn.execute(
                "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (registered["repository_id"],)
            ).fetchone()[0]
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO AI_MODELS (MODEL_ID,PROVIDER,MODEL_SLUG,DISPLAY_NAME,MODEL_ROLE,CAPABILITIES_JSON,"
                "ENABLED,PRIORITY,CREATED_AT,UPDATED_AT) VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("MODEL_SONNET", "OPENROUTER", "sonnet-5-free", "Sonnet 5", "REASONING", "[]", 1, 100, now, now),
            )

            for offset, hour in [(0.1, 10), (1.2, 10), (2.5, 14)]:
                conn.execute(
                    "INSERT INTO AI_REQUESTS (AI_REQUEST_ID,ORGANIZATION_ID,USER_ID,FEATURE,MODEL_ID,INPUT_HASH,"
                    "STATUS,LATENCY_MS,INPUT_TOKENS,OUTPUT_TOKENS,REASONING_TOKENS,CREATED_AT) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, self.organization_id, self.user["user_id"], "ENTERPRISE_COPILOT",
                     "MODEL_SONNET", "hash", "COMPLETED", 500, 100, 50, 0, _iso(offset, hour)),
                )
            conn.execute(
                "INSERT INTO AI_REQUESTS (AI_REQUEST_ID,ORGANIZATION_ID,USER_ID,FEATURE,MODEL_ID,INPUT_HASH,"
                "STATUS,LATENCY_MS,INPUT_TOKENS,OUTPUT_TOKENS,REASONING_TOKENS,CREATED_AT) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, self.organization_id, self.peer["user_id"], "ENTERPRISE_COPILOT",
                 "MODEL_SONNET", "hash", "COMPLETED", 400, 10, 5, 0, _iso(0.2, 9)),
            )

            for offset in [0.3, 3.0, 40.0]:
                conn.execute(
                    "INSERT INTO COMMITS (COMMIT_ID,REPOSITORY_ID,BRANCH_ID,AUTHOR_USER_ID,AUTHOR_EMAIL,MESSAGE,"
                    "CREATED_AT,CHANGE_COUNT,COMMIT_HASH,STATUS) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, "REPO_X", "BRANCH_X", self.user["user_id"], self.user["email"],
                     "test commit", _iso(offset, 10), 3, uuid.uuid4().hex, "COMMITTED"),
                )

            conn.execute(
                "INSERT INTO AUTH_SESSIONS (SESSION_ID,USER_ID,TOKEN_HASH,CREATED_AT,EXPIRES_AT) VALUES (?,?,?,?,?)",
                (uuid.uuid4().hex, self.user["user_id"], "hash", _iso(0.4, 10),
                 (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()),
            )

            branch_id = f"BR_{uuid.uuid4().hex[:10].upper()}"
            conn.execute(
                "INSERT INTO BRANCHES (BRANCH_ID,REPOSITORY_ID,DATA_TABLE_ID,BRANCH_NAME,BRANCH_TYPE,CREATED_BY,"
                "STATUS,CREATED_AT,UPDATED_AT) VALUES (?,?,?,?,?,?,?,?,?)",
                (branch_id, "REPO_X", f"TBL_{uuid.uuid4().hex[:10]}", "feature-branch", "USER",
                 self.user["user_id"], "ACTIVE", _iso(0.5, 10), _iso(0.5, 10)),
            )

            mr_id = f"MR_{uuid.uuid4().hex[:10].upper()}"
            conn.execute(
                "INSERT INTO MERGE_REQUESTS (MERGE_REQUEST_ID,REPOSITORY_ID,SOURCE_BRANCH_ID,TARGET_BRANCH_ID,"
                "SOURCE_HEAD_COMMIT_ID,TARGET_HEAD_COMMIT_ID,MERGE_BASE_COMMIT_ID,CREATED_BY,TITLE,STATUS,"
                "CONFLICT_STATUS,VALIDATION_STATUS,CREATED_AT,UPDATED_AT) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mr_id, "REPO_X", branch_id, "BRANCH_MAIN", "CMT_A", "CMT_B", "CMT_BASE",
                 self.user["user_id"], "Test MR", "MERGED", "NONE", "PASSED", _iso(0.5, 10), _iso(0.5, 10)),
            )
            conn.execute(
                "INSERT INTO MERGE_REQUEST_REVIEWS (REVIEW_ID,MERGE_REQUEST_ID,REVIEWER_USER_ID,DECISION,CREATED_AT) "
                "VALUES (?,?,?,?,?)",
                (uuid.uuid4().hex, mr_id, self.user["user_id"], "APPROVED", _iso(0.5, 11)),
            )

            conn.execute(
                "INSERT INTO AI_ACTIONS (ACTION_ID,ORGANIZATION_ID,USER_ID,ACTION_TYPE,RESOURCE_TYPE,"
                "PAYLOAD_OBJECT_HASH,RISK_LEVEL,REQUIRED_PERMISSION,STATUS,IDEMPOTENCY_KEY,EXPIRES_AT,"
                "CONFIRMED_BY,CONFIRMED_AT,CREATED_AT) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, self.organization_id, self.user["user_id"], "APPLY_MERGE_RESOLUTION",
                 "MERGE_CONFLICT", "hash", "LOW", "merge_request.resolve", "EXECUTED", uuid.uuid4().hex,
                 (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                 self.user["user_id"], _iso(0.5, 12), _iso(0.5, 12)),
            )

            conn.execute(
                "INSERT INTO EUC_ATTESTATIONS (ATTESTATION_ID,EUC_ID,REPOSITORY_ID,STATEMENT,SUBMITTED_BY,"
                "SUBMITTED_AT) VALUES (?,?,?,?,?,?)",
                (uuid.uuid4().hex, "EUC_X", "REPO_X", "Reviewed and acceptable", self.user["user_id"], _iso(0.5, 13)),
            )

            merge_agent_row = conn.execute(
                "SELECT AGENT_ID, NAME FROM AI_AGENTS WHERE AGENT_KEY='MERGE_CONFLICT_AGENT'"
            ).fetchone()
            self.merge_agent_name = merge_agent_row[1]
            conn.execute(
                "INSERT INTO AI_AGENT_RUNS (AGENT_RUN_ID,ORGANIZATION_ID,USER_ID,AGENT_ID,GOAL,RESOURCE_TYPE,"
                "STATUS,MAX_STEPS,CREATED_AT) VALUES (?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, self.organization_id, self.user["user_id"], merge_agent_row[0],
                 "resolve conflicts", "MERGE_REQUEST", "COMPLETED", 8, _iso(0.5, 14)),
            )
            conn.commit()
        finally:
            conn.close()

        from app.observability import record_audit_event
        record_audit_event("COMMIT_CREATED", actor_user_id=self.user["user_id"], actor_type="USER")
        record_audit_event("MERGE_REQUEST_APPROVED", actor_user_id=self.user["user_id"], actor_type="USER")
        record_audit_event("DEVICE_TRUSTED", actor_user_id=self.user["user_id"], actor_type="USER")

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_overview_counts_are_correct_for_the_default_30d_range(self):
        result = personal_activity(self.organization_id, self.user["user_id"], "30d")
        overview = result["overview"]
        # register_dataset() in setUp already creates one genesis commit for
        # this user, on top of the three explicit commits inserted above
        # (one of which, at 40 days old, falls outside the 30d window).
        self.assertEqual(overview["commits"], 3)
        self.assertEqual(overview["repositories"], 1)
        self.assertEqual(overview["repositories_touched"], 2)
        # The two in-range explicit commits (CHANGE_COUNT=3 each) plus
        # whatever the genesis commit itself touched.
        self.assertGreaterEqual(overview["cells_changed"], 6)
        self.assertEqual(overview["total_tokens"], 3 * (100 + 50))
        self.assertEqual(overview["peak_hour"], "10 AM")
        self.assertGreaterEqual(overview["active_days"], 2)
        self.assertGreaterEqual(overview["longest_streak"], 1)
        self.assertIsNotNone(overview["busiest_day"])

    def test_7d_range_excludes_older_activity(self):
        result = personal_activity(self.organization_id, self.user["user_id"], "7d")
        # Genesis commit (offset ~0) + the offset=0.3 and offset=3.0
        # commits; the offset=40.0 commit falls outside 7d.
        self.assertEqual(result["overview"]["commits"], 3)

    def test_heatmap_is_a_dense_daily_grid_covering_the_range(self):
        result = personal_activity(self.organization_id, self.user["user_id"], "30d")
        dates = [day["date"] for day in result["heatmap"]]
        self.assertEqual(len(dates), len(set(dates)))
        today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.assertIn(today_key, dates)
        total_from_heatmap = sum(day["count"] for day in result["heatmap"])
        self.assertEqual(total_from_heatmap, 6)

    def test_models_tab_breaks_down_by_model_with_success_rate(self):
        result = personal_activity(self.organization_id, self.user["user_id"], "30d")
        self.assertEqual(len(result["models"]), 1)
        model = result["models"][0]
        self.assertEqual(model["display_name"], "Sonnet 5")
        self.assertEqual(model["requests"], 3)
        self.assertEqual(model["success_rate"], 1.0)

    def test_longest_streak_and_busiest_day_are_computed_from_the_heatmap(self):
        result = personal_activity(self.organization_id, self.user["user_id"], "30d")
        heatmap = {day["date"]: day["count"] for day in result["heatmap"]}
        from app.ai.personal_activity import _busiest_day, _longest_streak
        expected_streak = _longest_streak(result["heatmap"])
        expected_busiest = _busiest_day(result["heatmap"])
        self.assertEqual(result["overview"]["longest_streak"], expected_streak)
        self.assertIn(str(expected_busiest["count"]), result["overview"]["busiest_day"])
        self.assertEqual(sum(heatmap.values()), sum(day["count"] for day in result["heatmap"]))

    def test_comparison_uses_real_peer_totals_not_a_fabricated_number(self):
        result = personal_activity(self.organization_id, self.user["user_id"], "30d")
        comparison = result["comparison"]
        self.assertIsNotNone(comparison["multiplier"])
        self.assertGreater(comparison["multiplier"], 1)
        self.assertIn("teammate", comparison["message"])

    def test_contributions_tab_counts_branches_and_merge_requests(self):
        result = personal_activity(self.organization_id, self.user["user_id"], "30d")
        contributions = result["contributions"]
        # register_dataset() in setUp already creates the repository's genesis
        # MAIN branch owned by this user, on top of the one USER branch
        # explicitly inserted above.
        self.assertEqual(contributions["branches_created"], 2)
        self.assertEqual(contributions["merge_requests_opened"], 1)
        self.assertEqual(contributions["merge_requests_merged"], 1)
        # REPO_X (used by the raw COMMITS/BRANCHES/MERGE_REQUESTS fixtures
        # above) is never registered in WORKBOOK_REPOSITORIES, so the
        # breakdown's join naturally excludes it; the genesis commit's real
        # repository from register_dataset() is what shows up here.
        self.assertTrue(len(contributions["repository_breakdown"]) >= 1)

    def test_reviews_tab_counts_reviews_confirmations_and_attestations(self):
        result = personal_activity(self.organization_id, self.user["user_id"], "30d")
        reviews = result["reviews"]
        self.assertEqual(reviews["reviews_given_total"], 1)
        self.assertEqual(reviews["reviews_given"].get("APPROVED"), 1)
        self.assertEqual(reviews["ai_actions_confirmed"], 1)
        self.assertEqual(reviews["attestations_submitted"], 1)

    def test_automation_tab_counts_agent_runs_by_agent_name(self):
        result = personal_activity(self.organization_id, self.user["user_id"], "30d")
        automation = result["automation"]
        self.assertEqual(automation["agent_runs_total"], 1)
        self.assertEqual(automation["agent_runs"][0]["agent_name"], self.merge_agent_name)

    def test_action_log_groups_audit_events_into_families(self):
        result = personal_activity(self.organization_id, self.user["user_id"], "30d")
        action_log = result["action_log"]
        self.assertGreaterEqual(action_log["by_family"].get("commit", 0), 1)
        self.assertGreaterEqual(action_log["by_family"].get("merge", 0), 1)
        self.assertGreaterEqual(action_log["by_family"].get("device", 0), 1)
        self.assertGreaterEqual(action_log["total_events"], 3)
        self.assertTrue(len(action_log["top_actions"]) > 0)


if __name__ == "__main__":
    unittest.main()
