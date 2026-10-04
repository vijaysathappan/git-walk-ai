import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import database
from app.integrations.canonical import create_mapping, create_schema
from app.integrations.search import operations_dashboard, search_catalogue
from app.integrations.service import integration_service
from app.integrations.thread import impact, thread_graph


class InformationFabricTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "information_fabric.db"
        database.initialize_product_schema()
        self.owner = database.get_or_create_user("fabric.owner@example.com")
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute('CREATE TABLE "QUEUE_BOARD_FABRIC" (ROW_ID INTEGER PRIMARY KEY, VALUE TEXT)')
        conn.execute('INSERT INTO "QUEUE_BOARD_FABRIC" VALUES (1, "seed")')
        conn.commit(); conn.close()
        self.repository = database.register_dataset("QUEUE_BOARD_FABRIC", self.owner["user_id"], "fabric.xlsx", 1, 1)
        conn = database._get_connection()
        self.organization_id = conn.execute(
            "SELECT ORGANIZATION_ID FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?", (self.repository["repository_id"],)
        ).fetchone()[0]
        conn.close()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def test_file_ingestion_canonical_mapping_lineage_and_idempotency(self):
        connection = integration_service.create_connection(self.organization_id, {
            "name": "Customer landing", "connector_type": "FILE", "environment": "TEST",
            "configuration": {"format": "CSV", "object_type": "Customer", "id_field": "customer_id"},
            "credentials": {"source_password": "never-return-this"},
        }, self.owner["user_id"])
        self.assertTrue(connection["credential_configured"])
        self.assertNotIn("credentials", connection)
        payload = b"customer_id,name,country\nC-1,Ada,GB\nC-2,Grace,US\n"
        source = integration_service.upload_source(
            self.organization_id, connection["connection_id"], "customers.csv", payload, self.owner["user_id"],
        )
        self.assertEqual(len(payload), source["bytes"])
        test = await integration_service.test_connection(self.organization_id, connection["connection_id"], self.owner["user_id"])
        self.assertEqual("HEALTHY", test["health_status"])
        discovery = await integration_service.discover(self.organization_id, connection["connection_id"], self.owner["user_id"])
        self.assertEqual({"customer_id", "name", "country"}, {field["name"] for field in discovery["schema"]["fields"]})
        schema = create_schema(self.organization_id, "EnterpriseCustomer", {
            "fields": {
                "customer_id": {"type": "STRING", "required": True},
                "display_name": {"type": "STRING", "required": True},
                "country": {"type": "STRING"},
            }
        }, ["customer_id"], self.owner["user_id"])
        mapping = create_mapping(self.organization_id, connection["connection_id"], "Customer", schema["schema_id"], {
            "fields": {
                "customer_id": "customer_id", "display_name": {"source": "name", "transforms": ["trim"]},
                "country": {"source": "country", "transforms": ["upper"]},
            }
        }, self.owner["user_id"])
        run = await integration_service.execute(
            self.organization_id, connection["connection_id"], self.owner["user_id"],
            mapping_id=mapping["mapping_id"], idempotency_key="customers:initial", batch_size=100,
        )
        self.assertEqual("COMPLETED", run["status"])
        self.assertEqual(2, run["records_ingested"])
        self.assertEqual(2, run["records_mapped"])
        self.assertEqual("MATCHED", run["reconciliation"]["status"])
        replay = await integration_service.execute(
            self.organization_id, connection["connection_id"], self.owner["user_id"],
            mapping_id=mapping["mapping_id"], idempotency_key="customers:initial",
        )
        self.assertTrue(replay["idempotent_replay"])
        conn = database._get_connection()
        try:
            self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM RAW_INGESTION_ENVELOPES").fetchone()[0])
            self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM CANONICAL_OBJECTS").fetchone()[0])
            encrypted = conn.execute("SELECT ENCRYPTED_VALUE FROM INTEGRATION_SECRETS").fetchone()[0]
            self.assertNotIn("never-return-this", encrypted)
            node_id = conn.execute("SELECT NODE_ID FROM DIGITAL_THREAD_NODES WHERE NODE_TYPE='INTEGRATION_CONNECTION'").fetchone()[0]
        finally: conn.close()
        graph = thread_graph(self.organization_id, node_id, 5)
        self.assertGreaterEqual(len(graph["nodes"]), 4)
        self.assertGreaterEqual(impact(self.organization_id, node_id, 5)["affected_count"], 1)
        self.assertGreaterEqual(search_catalogue(self.organization_id, self.owner["user_id"], "customer")["count"], 1)
        operations = operations_dashboard(self.organization_id, self.owner["user_id"])
        self.assertEqual(1, operations["connections"]["healthy"])
        self.assertEqual(2, operations["canonical"]["objects"])

    async def test_invalid_canonical_record_is_quarantined(self):
        connection = integration_service.create_connection(self.organization_id, {
            "name": "Invalid landing", "connector_type": "FILE", "configuration": {"format": "JSON", "object_type": "Customer"},
        }, self.owner["user_id"])
        integration_service.upload_source(self.organization_id, connection["connection_id"], "invalid.json", b'[{"name":"Missing ID"}]', self.owner["user_id"])
        schema = create_schema(self.organization_id, "StrictCustomer", {"fields": {"customer_id": {"type": "STRING", "required": True}}}, ["customer_id"], self.owner["user_id"])
        mapping = create_mapping(self.organization_id, connection["connection_id"], "Customer", schema["schema_id"], {"fields": {"customer_id": "customer_id"}}, self.owner["user_id"])
        run = await integration_service.execute(self.organization_id, connection["connection_id"], self.owner["user_id"], mapping_id=mapping["mapping_id"])
        self.assertEqual("COMPLETED_WITH_WARNINGS", run["status"])
        self.assertEqual(1, run["quarantined"])
        conn = database._get_connection()
        try: self.assertEqual("OPEN", conn.execute("SELECT STATUS FROM CANONICAL_QUARANTINE").fetchone()[0])
        finally: conn.close()


if __name__ == "__main__":
    unittest.main()
