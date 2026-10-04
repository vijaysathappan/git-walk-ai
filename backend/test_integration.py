"""Integration test for the ExcelSQLiteLiveSync API endpoints."""
import requests
import sqlite3

# === Step 1: Upload test file ===
print("=" * 60)
print("STEP 1: Upload & Provision")
print("=" * 60)

with open("test_upload.xlsx", "rb") as f:
    resp = requests.post(
        "http://localhost:8000/api/v1/upload-and-provision",
        files={"file": ("test_upload.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )

print(f"Status: {resp.status_code}")
print(f"X-Table-ID: {resp.headers.get('X-Table-ID')}")
print(f"X-Row-Count: {resp.headers.get('X-Row-Count')}")
print(f"X-Column-Count: {resp.headers.get('X-Column-Count')}")
print(f"Content-Length: {len(resp.content)} bytes")

assert resp.status_code == 200, f"Upload failed: {resp.text}"

table_id = resp.headers.get("X-Table-ID")
with open("configured_test.xlsx", "wb") as out:
    out.write(resp.content)
print("Saved configured_test.xlsx")

# === Step 2: Verify SQLite table ===
print("\n" + "=" * 60)
print("STEP 2: Verify SQLite Database")
print("=" * 60)

conn = sqlite3.connect("queue_board.db")
cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
tables = [r[0] for r in cursor.fetchall()]
print(f"Tables in DB: {tables}")

cursor = conn.execute(f'SELECT * FROM "{table_id}";')
cols = [d[0] for d in cursor.description]
rows = cursor.fetchall()
print(f"Columns: {cols}")
print(f"Row count: {len(rows)}")
for r in rows:
    print(f"  {r}")

# === Step 3: Test real-time sync ===
print("\n" + "=" * 60)
print("STEP 3: Real-Time Sync")
print("=" * 60)

# Find first non-ROW_ID column
sync_col = cols[1]
sync_resp = requests.post(
    "http://localhost:8000/api/v1/realtime-sync",
    json={
        "table_id": table_id,
        "row_id": 1,
        "column_name": sync_col,
        "new_value": "UPDATED_VALUE",
    },
)
print(f"Sync Status: {sync_resp.status_code}")
print(f"Sync Response: {sync_resp.json()}")

assert sync_resp.status_code == 200, f"Sync failed: {sync_resp.text}"

# Verify the update
cursor = conn.execute(f'SELECT * FROM "{table_id}" WHERE ROW_ID = 1;')
updated_row = cursor.fetchone()
print(f"After sync (ROW_ID=1): {updated_row}")

conn.close()

print("\n" + "=" * 60)
print("ALL TESTS PASSED ✓")
print("=" * 60)
