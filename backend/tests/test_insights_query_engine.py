import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import database
from app.ai.insights_query_engine import (
    QuerySpecError,
    compile_to_sql,
    discover_schema,
    execute,
    linear_trend,
    validate_query_spec,
)


class InsightsQueryEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "insights_query_engine.db"
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute(
            'CREATE TABLE "QUEUE_BOARD_SALES" (ROW_ID INTEGER PRIMARY KEY, "REGION" TEXT, "AMOUNT" REAL)'
        )
        conn.executemany(
            'INSERT INTO "QUEUE_BOARD_SALES" (ROW_ID, "REGION", "AMOUNT") VALUES (?, ?, ?)',
            [(1, "West", 100.0), (2, "West", 50.0), (3, "East", 75.0), (4, "East", 25.0), (5, "North", 10.0)],
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_discover_schema_reads_real_columns_and_samples_low_cardinality_text(self):
        schema = discover_schema("QUEUE_BOARD_SALES")
        names = {c.name for c in schema.columns}
        self.assertEqual({"REGION", "AMOUNT"}, names)
        self.assertEqual(5, schema.row_count)
        region = schema.find("region")
        self.assertFalse(region.is_numeric)
        self.assertEqual({"West", "East", "North"}, set(region.sample_values))
        amount = schema.find("AMOUNT")
        self.assertTrue(amount.is_numeric)

    def test_discover_schema_rejects_a_nonexistent_table(self):
        with self.assertRaises(QuerySpecError):
            discover_schema("QUEUE_BOARD_DOES_NOT_EXIST")

    def test_validate_query_spec_rejects_an_unknown_column(self):
        schema = discover_schema("QUEUE_BOARD_SALES")
        with self.assertRaises(QuerySpecError):
            validate_query_spec({"group_by": "NOT_A_COLUMN", "aggregation": "SUM", "aggregation_column": "AMOUNT"}, schema)

    def test_validate_query_spec_rejects_an_unknown_aggregation(self):
        schema = discover_schema("QUEUE_BOARD_SALES")
        with self.assertRaises(QuerySpecError):
            validate_query_spec({"aggregation": "DROP_TABLE", "aggregation_column": "AMOUNT"}, schema)

    def test_validate_query_spec_rejects_sum_over_a_non_numeric_column(self):
        schema = discover_schema("QUEUE_BOARD_SALES")
        with self.assertRaises(QuerySpecError):
            validate_query_spec({"aggregation": "SUM", "aggregation_column": "REGION"}, schema)

    def test_validate_query_spec_rejects_an_unknown_filter_operator(self):
        schema = discover_schema("QUEUE_BOARD_SALES")
        with self.assertRaises(QuerySpecError):
            validate_query_spec({"filters": [{"column": "REGION", "op": "; DROP TABLE", "value": "West"}]}, schema)

    def test_compile_and_execute_group_by_sum(self):
        schema = discover_schema("QUEUE_BOARD_SALES")
        spec = validate_query_spec({"group_by": "REGION", "aggregation": "SUM", "aggregation_column": "AMOUNT", "chart_type": "BAR"}, schema)
        sql, params = compile_to_sql(spec, schema)
        rows = execute(sql, params)
        by_region = {row["group_key"]: row["value"] for row in rows}
        self.assertEqual({"West": 150.0, "East": 100.0, "North": 10.0}, by_region)

    def test_compile_and_execute_count_with_filter(self):
        schema = discover_schema("QUEUE_BOARD_SALES")
        spec = validate_query_spec(
            {"group_by": "REGION", "aggregation": "COUNT", "filters": [{"column": "REGION", "op": "!=", "value": "North"}], "chart_type": "BAR"},
            schema,
        )
        sql, params = compile_to_sql(spec, schema)
        rows = execute(sql, params)
        by_region = {row["group_key"]: row["value"] for row in rows}
        self.assertEqual({"West": 2, "East": 2}, by_region)
        self.assertNotIn("North", by_region)

    def test_linear_trend_detects_an_upward_series(self):
        rows = [{"group_key": "Jan", "value": 10}, {"group_key": "Feb", "value": 20}, {"group_key": "Mar", "value": 30}]
        trend = linear_trend(rows)
        self.assertIsNotNone(trend)
        self.assertEqual("up", trend["direction"])
        self.assertIn("disclaimer", trend)

    def test_linear_trend_returns_none_for_too_few_points(self):
        rows = [{"group_key": "Jan", "value": 10}, {"group_key": "Feb", "value": 20}]
        self.assertIsNone(linear_trend(rows))


if __name__ == "__main__":
    unittest.main()
