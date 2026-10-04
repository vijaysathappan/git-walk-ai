"""
OpenXML Injector Engine — Injects Office Web Add-in Taskpane into .xlsx Files.

This module manipulates the raw ZIP/OpenXML structure of an Excel workbook to
embed a Web Add-in manifest so that when the file is opened in Excel Desktop,
a side-panel (taskpane) automatically loads the React sync UI.
"""

import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import defusedxml.ElementTree as DET

from .config import settings

# ---------------------------------------------------------------------------
# XML namespace constants used by OpenXML & Office Web Extensions
# ---------------------------------------------------------------------------
NS = {
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "we": "http://schemas.microsoft.com/office/webextensions/webextension/2010/11",
    "tp": "http://schemas.microsoft.com/office/webextensions/taskpanes/2010/11",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}

TABLE_ID_DEFINED_NAME = "_EXCEL_SQLITE_SYNC_TABLE_ID"
# This must stay identical to frontend/public/manifest.xml. Office resolves an
# embedded task pane by manifest ID; a per-workbook UUID can never be resolved.
OFFICE_ADDIN_ID = "e4f5a6b7-c8d9-0e1f-2a3b-4c5d6e7f8a9b"
WORKBOOK_METADATA_NAMES = {
    "repository_id": "_GITWALK_REPOSITORY_ID",
    "branch_id": "_GITWALK_BRANCH_ID",
    "branch_name": "_GITWALK_BRANCH_NAME",
    "working_copy_id": "_GITWALK_WORKING_COPY_ID",
    "base_commit_id": "_GITWALK_BASE_COMMIT_ID",
    "issued_at": "_GITWALK_ISSUED_AT",
    "signature": "_GITWALK_SIGNATURE",
    "local_file_path": "_GITWALK_LOCAL_FILE_PATH",
    "required_role": "_GITWALK_REQUIRED_ROLE",
    "assigned_email": "_GITWALK_ASSIGNED_EMAIL",
}

# Register all namespaces so ET doesn't mangle prefixes
for prefix, uri in NS.items():
    ET.register_namespace(prefix if prefix != "ct" else "", uri)
# Also register common spreadsheetml namespaces to preserve them
ET.register_namespace("", "http://schemas.openxmlformats.org/spreadsheetml/2006/main")
ET.register_namespace("r", "http://schemas.openxmlformats.org/officeDocument/2006/relationships")


def inject_taskpane_manifest(
    input_xlsx_path: str,
    output_xlsx_path: str,
    manifest_url: str = f"{settings.office_addin_url}/taskpane.html",
    table_id: str | None = None,
    metadata: dict[str, str] | None = None,
) -> None:
    """
    Inject a Web Add-in taskpane into an existing .xlsx file.

    Steps:
        1. Extract the .xlsx (ZIP) to a temp directory
        2. Patch [Content_Types].xml with webextension + taskpane overrides
        3. Create xl/webextensions/webextension1.xml
        4. Create xl/webextensions/_rels/webextension1.xml.rels
        5. Create xl/taskpanes/taskpane1.xml
        6. Create xl/taskpanes/_rels/taskpane1.xml.rels
        7. Update xl/_rels/workbook.xml.rels with taskpane relationship
        8. Repackage as .xlsx without corrupting existing content

    Parameters
    ----------
    input_xlsx_path : str
        Path to the original uploaded .xlsx file.
    output_xlsx_path : str
        Path where the modified .xlsx will be written.
    manifest_url : str
        The HTTPS URL of the taskpane HTML page.
    table_id : str | None
        SQLite table identifier embedded into the add-in settings.
    """
    tmp_dir = tempfile.mkdtemp(prefix="xlsx_inject_")

    try:
        # ── Step 1: Extract ──────────────────────────────────────────────
        with zipfile.ZipFile(input_xlsx_path, "r") as zin:
            zin.extractall(tmp_dir)

        # ── Step 2: Patch [Content_Types].xml ────────────────────────────
        _patch_content_types(tmp_dir)

        defined_values = dict(metadata or {})
        if table_id:
            defined_values["table_id"] = table_id
        if defined_values:
            _embed_workbook_metadata(tmp_dir, defined_values)

        # ── Step 3-4: Create webextension ────────────────────────────────
        _create_webextension(tmp_dir, manifest_url, table_id, metadata)

        # ── Step 5-6: Create taskpane ────────────────────────────────────
        _create_taskpane(tmp_dir)

        # ── Step 7: Update workbook.xml.rels ─────────────────────────────
        _update_workbook_rels(tmp_dir)

        # ── Step 8: Repackage ────────────────────────────────────────────
        _repackage_xlsx(tmp_dir, output_xlsx_path)

    finally:
        # Clean up temp directory
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════

def _embed_workbook_metadata(tmp_dir: str, metadata: dict[str, str]) -> None:
    """Store signed Git Walk identity as hidden workbook-level defined names."""
    workbook_path = os.path.join(tmp_dir, "xl", "workbook.xml")
    tree = DET.parse(workbook_path)
    root = tree.getroot()
    main_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    defined_names_tag = f"{{{main_ns}}}definedNames"
    defined_name_tag = f"{{{main_ns}}}definedName"

    defined_names = root.find(defined_names_tag)
    if defined_names is None:
        defined_names = ET.Element(defined_names_tag)
        insert_before = {
            "calcPr",
            "oleSize",
            "customWorkbookViews",
            "pivotCaches",
            "webPublishing",
            "fileRecoveryPr",
            "webPublishObjects",
            "extLst",
        }
        insert_at = len(root)
        for index, child in enumerate(root):
            if child.tag.rsplit("}", 1)[-1] in insert_before:
                insert_at = index
                break
        root.insert(insert_at, defined_names)

    name_map = {"table_id": TABLE_ID_DEFINED_NAME, **WORKBOOK_METADATA_NAMES}
    names_to_replace = {
        name_map[key] for key, value in metadata.items() if key in name_map and value is not None
    }
    for existing in list(defined_names.findall(defined_name_tag)):
        if existing.get("name") in names_to_replace:
            defined_names.remove(existing)

    for key, defined_name in name_map.items():
        value = metadata.get(key)
        if value is None:
            continue
        item = ET.SubElement(
            defined_names,
            defined_name_tag,
            attrib={"name": defined_name, "hidden": "1"},
        )
        item.text = f'"{str(value).replace(chr(34), chr(34) * 2)}"'
    tree.write(workbook_path, xml_declaration=True, encoding="UTF-8")

def _patch_content_types(tmp_dir: str) -> None:
    """Add Override entries for webextension and taskpane content types."""
    ct_path = os.path.join(tmp_dir, "[Content_Types].xml")
    tree = DET.parse(ct_path)
    root = tree.getroot()

    # The default namespace for Content_Types is the 'ct' namespace
    ct_ns = "http://schemas.openxmlformats.org/package/2006/content-types"

    overrides_to_add = [
        {
            "PartName": "/xl/webextensions/webextension1.xml",
            "ContentType": "application/vnd.ms-office.webextension+xml",
        },
        {
            "PartName": "/xl/taskpanes/taskpane1.xml",
            "ContentType": "application/vnd.ms-office.taskpanes+xml",
        },
    ]

    existing_parts = {
        el.get("PartName")
        for el in root.findall(f"{{{ct_ns}}}Override")
    }

    for override in overrides_to_add:
        if override["PartName"] not in existing_parts:
            ET.SubElement(root, f"{{{ct_ns}}}Override", attrib=override)

    tree.write(ct_path, xml_declaration=True, encoding="UTF-8")


def _create_webextension(
    tmp_dir: str,
    manifest_url: str,
    table_id: str | None = None,
    metadata: dict[str, str] | None = None,
) -> None:
    """Create xl/webextensions/webextension1.xml and its .rels file."""
    we_dir = os.path.join(tmp_dir, "xl", "webextensions")
    os.makedirs(we_dir, exist_ok=True)

    we_ns = NS["we"]
    addin_id = OFFICE_ADDIN_ID

    # ── webextension1.xml ────────────────────────────────────────────────
    we_root = ET.Element(f"{{{we_ns}}}webextension", attrib={"id": addin_id})

    # Reference to the add-in (developer catalog)
    ref = ET.SubElement(
        we_root,
        f"{{{we_ns}}}reference",
        attrib={
            "id": addin_id,
            "version": "1.0.0.0",
            "store": "developer",
            "storeType": "Registry",
        },
    )

    # Alternate references (empty)
    ET.SubElement(we_root, f"{{{we_ns}}}alternateReferences")

    # Properties — store the manifest URL for convenience
    props = ET.SubElement(we_root, f"{{{we_ns}}}properties")
    ET.SubElement(
        props,
        f"{{{we_ns}}}property",
        attrib={"name": "manifestUrl", "value": manifest_url},
    )
    if table_id:
        ET.SubElement(
            props,
            f"{{{we_ns}}}property",
            attrib={"name": "tableId", "value": table_id},
        )
    for key, value in (metadata or {}).items():
        if key not in WORKBOOK_METADATA_NAMES or value is None:
            continue
        ET.SubElement(
            props,
            f"{{{we_ns}}}property",
            attrib={"name": key, "value": str(value)},
        )

    # Bindings (empty)
    ET.SubElement(we_root, f"{{{we_ns}}}bindings")

    # Snapshot (empty)
    ET.SubElement(we_root, f"{{{we_ns}}}snapshot")

    we_tree = ET.ElementTree(we_root)
    we_path = os.path.join(we_dir, "webextension1.xml")
    we_tree.write(we_path, xml_declaration=True, encoding="UTF-8")

    # ── _rels/webextension1.xml.rels ─────────────────────────────────────
    rels_dir = os.path.join(we_dir, "_rels")
    os.makedirs(rels_dir, exist_ok=True)

    rel_ns = NS["rel"]
    rels_root = ET.Element(f"{{{rel_ns}}}Relationships")
    # No external relationships needed for a developer-catalog add-in
    rels_tree = ET.ElementTree(rels_root)
    rels_tree.write(
        os.path.join(rels_dir, "webextension1.xml.rels"),
        xml_declaration=True,
        encoding="UTF-8",
    )


def _create_taskpane(tmp_dir: str) -> None:
    """Create xl/taskpanes/taskpane1.xml and its .rels file."""
    tp_dir = os.path.join(tmp_dir, "xl", "taskpanes")
    os.makedirs(tp_dir, exist_ok=True)

    tp_ns = NS["tp"]

    # ── taskpane1.xml ────────────────────────────────────────────────────
    tp_root = ET.Element(f"{{{tp_ns}}}taskpanes")
    taskpane_el = ET.SubElement(
        tp_root,
        f"{{{tp_ns}}}taskpane",
        attrib={
            "dockstate": "right",
            "visibility": "1",
            "width": "350",
            "row": "0",
        },
    )
    # Link to webextension via relationship id
    ET.SubElement(
        taskpane_el,
        f"{{{tp_ns}}}webextensionref",
        attrib={
            f"{{{NS['r']}}}id": "rId1",
        },
    )

    tp_tree = ET.ElementTree(tp_root)
    tp_path = os.path.join(tp_dir, "taskpane1.xml")
    tp_tree.write(tp_path, xml_declaration=True, encoding="UTF-8")

    # ── _rels/taskpane1.xml.rels ─────────────────────────────────────────
    rels_dir = os.path.join(tp_dir, "_rels")
    os.makedirs(rels_dir, exist_ok=True)

    rel_ns = NS["rel"]
    rels_root = ET.Element(f"{{{rel_ns}}}Relationships")
    ET.SubElement(
        rels_root,
        f"{{{rel_ns}}}Relationship",
        attrib={
            "Id": "rId1",
            "Type": "http://schemas.microsoft.com/office/2011/relationships/webextension",
            "Target": "../webextensions/webextension1.xml",
        },
    )
    rels_tree = ET.ElementTree(rels_root)
    rels_tree.write(
        os.path.join(rels_dir, "taskpane1.xml.rels"),
        xml_declaration=True,
        encoding="UTF-8",
    )


def _update_workbook_rels(tmp_dir: str) -> None:
    """Add a relationship from workbook.xml to taskpanes/taskpane1.xml."""
    rels_path = os.path.join(tmp_dir, "xl", "_rels", "workbook.xml.rels")

    rel_ns = NS["rel"]
    tree = DET.parse(rels_path)
    root = tree.getroot()

    # Determine next available rId
    existing_ids = {
        el.get("Id") for el in root.findall(f"{{{rel_ns}}}Relationship")
    }
    rid_num = 1
    while f"rId{rid_num}" in existing_ids:
        rid_num += 1
    new_rid = f"rId{rid_num}"

    # Check if the taskpane relationship already exists
    taskpane_type = (
        "http://schemas.microsoft.com/office/2011/relationships/webextensionTaskPanes"
    )
    for rel in root.findall(f"{{{rel_ns}}}Relationship"):
        if rel.get("Type") == taskpane_type:
            return  # already injected

    ET.SubElement(
        root,
        f"{{{rel_ns}}}Relationship",
        attrib={
            "Id": new_rid,
            "Type": taskpane_type,
            "Target": "taskpanes/taskpane1.xml",
        },
    )

    tree.write(rels_path, xml_declaration=True, encoding="UTF-8")


def _repackage_xlsx(tmp_dir: str, output_path: str) -> None:
    """
    Repackage the extracted directory back into a .xlsx ZIP file.

    Uses ZIP_DEFLATED compression to keep the file compact.
    Preserves all existing content (formulas, cell formats, charts, etc.).
    """
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for root_dir, dirs, files in os.walk(tmp_dir):
            for file in files:
                abs_path = os.path.join(root_dir, file)
                arc_name = os.path.relpath(abs_path, tmp_dir)
                zout.write(abs_path, arc_name)
