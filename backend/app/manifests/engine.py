"""Fixed-size Merkle-style workbook manifests with copy-on-write deduplication."""

from typing import Any

from ..config import settings
from ..storage.service import ObjectService


class ManifestEngine:
    def __init__(self, objects: ObjectService, block_rows: int | None = None):
        self.objects = objects
        self.block_rows = block_rows or settings.semantic_block_rows

    def _reference(self, conn, parent: str, child: str, kind: str, position: int) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO OBJECT_REFERENCES (PARENT_HASH,CHILD_HASH,REFERENCE_TYPE,POSITION) VALUES (?,?,?,?)",
            (parent, child, kind, position),
        )

    def build(self, conn, state: dict[str, Any]) -> dict[str, Any]:
        created = reused = raw_bytes = stored_bytes = 0

        def write(object_type: str, payload: Any) -> dict[str, Any]:
            nonlocal created, reused, raw_bytes, stored_bytes
            result = self.objects.put(conn, object_type, payload)
            created += int(result["created"])
            reused += int(not result["created"])
            if result["created"]:
                raw_bytes += result["raw_size"]
                stored_bytes += result["compressed_size"]
            return result

        sheet_refs = []
        for sheet in sorted(state.get("sheets", []), key=lambda item: item.get("position", 0)):
            columns = write("COLUMN_MANIFEST", {
                "version": 1, "sheet_id": sheet["sheet_id"], "columns": sheet.get("columns", []),
            })
            row_blocks = []
            formula_blocks = []
            style_blocks = []
            comment_blocks = []
            rows = sorted(sheet.get("rows", []), key=lambda item: item.get("position", 0))
            for block_index, start in enumerate(range(0, len(rows), self.block_rows)):
                block_rows = rows[start:start + self.block_rows]
                block = write("VALUE_BLOCK", {
                    "version": 1, "sheet_id": sheet["sheet_id"], "block_index": block_index,
                    "row_start": start, "row_end": start + len(block_rows),
                    "rows": [{key: row.get(key) for key in ("row_id", "physical_row_id", "position", "values")} for row in block_rows],
                })
                row_blocks.append(block["object_hash"])
                sparse_kinds = (
                    ("FORMULA_BLOCK", "formulas", formula_blocks),
                    ("STYLE_BLOCK", "styles", style_blocks),
                    ("COMMENT_BLOCK", "comments", comment_blocks),
                )
                for object_type, field, target in sparse_kinds:
                    sparse = [
                        {"row_id": row.get("row_id"), field: row.get(field, {})}
                        for row in block_rows if row.get(field)
                    ]
                    if sparse:
                        result = write(object_type, {
                            "version": 1, "sheet_id": sheet["sheet_id"],
                            "block_index": block_index, "rows": sparse,
                        })
                        target.append(result["object_hash"])
            sheet_manifest = {
                "version": 1, "sheet_id": sheet["sheet_id"], "name": sheet.get("name", "Sheet"),
                "position": sheet.get("position", 0), "column_manifest": columns["object_hash"],
                "value_blocks": row_blocks, "formula_blocks": formula_blocks,
                "style_blocks": style_blocks, "comment_blocks": comment_blocks,
                "row_count": len(rows), "block_rows": self.block_rows,
            }
            sheet_result = write("SHEET_MANIFEST", sheet_manifest)
            self._reference(conn, sheet_result["object_hash"], columns["object_hash"], "COLUMNS", 0)
            for position, child in enumerate(row_blocks):
                self._reference(conn, sheet_result["object_hash"], child, "VALUE_BLOCK", position)
            for reference_type, children in (
                ("FORMULA_BLOCK", formula_blocks), ("STYLE_BLOCK", style_blocks),
                ("COMMENT_BLOCK", comment_blocks),
            ):
                for position, child in enumerate(children):
                    self._reference(conn, sheet_result["object_hash"], child, reference_type, position)
            sheet_refs.append({"sheet_id": sheet["sheet_id"], "manifest": sheet_result["object_hash"]})
        root = write("ROOT_MANIFEST", {
            "version": 1, "repository_id": state.get("repository_id"), "sheets": sheet_refs,
        })
        for position, child in enumerate(sheet_refs):
            self._reference(conn, root["object_hash"], child["manifest"], "SHEET", position)
        return {
            "root_manifest_hash": root["object_hash"], "objects_created": created,
            "objects_reused": reused, "new_physical_bytes": stored_bytes,
            "logical_changed_bytes": raw_bytes,
        }

    def hydrate(self, conn, root_hash: str) -> dict[str, Any]:
        root = self.objects.get(conn, root_hash)
        sheets = []
        for sheet_ref in root.get("sheets", []):
            manifest = self.objects.get(conn, sheet_ref["manifest"])
            columns = self.objects.get(conn, manifest["column_manifest"])["columns"]
            rows = []
            for block_hash in manifest.get("value_blocks", []):
                rows.extend(self.objects.get(conn, block_hash).get("rows", []))
            rows_by_id = {row["row_id"]: row for row in rows}
            for field, key in (
                ("formula_blocks", "formulas"), ("style_blocks", "styles"),
                ("comment_blocks", "comments"),
            ):
                for block_hash in manifest.get(field, []):
                    for sparse in self.objects.get(conn, block_hash).get("rows", []):
                        if sparse["row_id"] in rows_by_id:
                            rows_by_id[sparse["row_id"]][key] = sparse.get(key, {})
            for row in rows:
                # Canonical JSON sorts object keys for hashing. Restore workbook column
                # order at the read boundary so hydrated state remains Excel-shaped.
                values = row.get("values", {})
                row["values"] = {
                    column["column_id"]: values.get(column["column_id"])
                    for column in columns
                    if column["column_id"] in values
                }
                row.setdefault("formulas", {})
                row.setdefault("styles", {})
                row.setdefault("comments", {})
            sheets.append({
                "sheet_id": manifest["sheet_id"], "name": manifest["name"],
                "position": manifest["position"], "columns": columns, "rows": rows,
            })
        return {"repository_id": root.get("repository_id"), "sheets": sheets}
