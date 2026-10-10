import React, { useCallback, useEffect, useRef, useState } from "react";
import CommitReviewPanel from "./CommitReviewPanel";
import StatusBanner from "./StatusBanner";
import {
  authenticateWorkbook,
  clearAuth,
  commitWorkbook,
  getBranchState,
  getBranchDivergence,
  getClientId,
  getDeviceId,
  getStoredAuth,
  getWorkbookSnapshot,
  heartbeatPresence,
  leaveDatasetPresence,
  logout,
  requestLoginCode,
  syncBranchWithMain,
  verifyLoginCode,
  verifyWorkbookAccess,
  sanitizeLocalWorkbook,
  setUserPassword,
  startFormulaExplanation,
  getFormulaExplanation,
  getAgentRunProgress,
} from "../services/api";

const TABLE_ID_DEFINED_NAME = "_EXCEL_SQLITE_SYNC_TABLE_ID";
const WORKBOOK_METADATA_NAMES = {
  repository_id: "_GITWALK_REPOSITORY_ID",
  branch_id: "_GITWALK_BRANCH_ID",
  branch_name: "_GITWALK_BRANCH_NAME",
  working_copy_id: "_GITWALK_WORKING_COPY_ID",
  base_commit_id: "_GITWALK_BASE_COMMIT_ID",
  issued_at: "_GITWALK_ISSUED_AT",
  signature: "_GITWALK_SIGNATURE",
  local_file_path: "_GITWALK_LOCAL_FILE_PATH",
  required_role: "_GITWALK_REQUIRED_ROLE",
  assigned_email: "_GITWALK_ASSIGNED_EMAIL",
};
const ROW_ID_HEADER = "__GITWALK_ROW_ID";
const LEGACY_ROW_ID_HEADER = "__LIVESYNC_ROW_ID";

function normalizeFilePath(rawPath) {
  if (!rawPath || typeof rawPath !== "string") return "";
  let decoded = "";
  try {
    decoded = decodeURIComponent(rawPath.trim());
  } catch {
    decoded = rawPath.trim();
  }
  if (decoded.toLowerCase().startsWith("file:///")) {
    decoded = decoded.slice(8);
  } else if (decoded.toLowerCase().startsWith("file://")) {
    decoded = decoded.slice(7);
  }
  decoded = decoded.replace(/\//g, "\\");
  while (decoded.length >= 3 && decoded[0] === "\\" && decoded[2] === ":") {
    decoded = decoded.slice(1);
  }
  return decoded.replace(/\\+$/, "").toLowerCase();
}

function clientStableId(prefix) {
  const random = globalThis.crypto?.randomUUID?.().replace(/-/g, "").toUpperCase()
    || `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`.toUpperCase();
  return `${prefix}_${random.slice(0, 20)}`;
}

function sanitizeColumnName(raw) {
  let name = String(raw || "")
    .trim()
    .toUpperCase()
    .replace(/ /g, "_")
    .replace(/[^A-Z0-9_]/g, "_")
    .replace(/_+/g, "_")
    .replace(/^_+|_+$/g, "");
  if (!name) name = "COL";
  if (/^\d/.test(name)) name = `_${name}`;
  return name.substring(0, 30);
}

function normalizeValue(value) {
  return value === "" || value == null ? null : value;
}

const EXCEL_EPOCH_MS = Date.UTC(1899, 11, 30);

function dateTextMilliseconds(value) {
  if (typeof value !== "string") return null;
  const match = value.trim().match(
    /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?(?:Z|[+-]\d{2}:?\d{2})?)?$/
  );
  if (!match) return null;
  const milliseconds = Number((match[7] || "").padEnd(3, "0").slice(0, 3));
  return Date.UTC(
    Number(match[1]), Number(match[2]) - 1, Number(match[3]),
    Number(match[4] || 0), Number(match[5] || 0), Number(match[6] || 0), milliseconds
  );
}

function excelSerialMilliseconds(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 1 || value > 2958465) return null;
  return EXCEL_EPOCH_MS + value * 86400000;
}

function formatExcelDateLike(serial, reference) {
  const milliseconds = excelSerialMilliseconds(serial);
  if (milliseconds == null) return serial;
  const date = new Date(milliseconds);
  const datePart = date.toISOString().slice(0, 10);
  if (typeof reference === "string" && /^\d{4}-\d{2}-\d{2}$/.test(reference.trim())) return datePart;
  const separator = typeof reference === "string" && reference.includes("T") ? "T" : " ";
  return `${datePart}${separator}${date.toISOString().slice(11, 19)}`;
}

function normalizeSemanticValue(value, reference = null, dataType = "") {
  const normalized = normalizeValue(value);
  if (
    typeof normalized === "number"
    && (dateTextMilliseconds(reference) != null || /DATE|TIME/i.test(String(dataType)))
  ) {
    return formatExcelDateLike(normalized, reference);
  }
  return normalized;
}

function valuesEqual(left, right) {
  if (left == null || left === "") return right == null || right === "";
  if (right == null || right === "") return false;
  const leftDate = dateTextMilliseconds(left);
  const rightDate = dateTextMilliseconds(right);
  if (leftDate != null && rightDate != null) return Math.abs(leftDate - rightDate) < 1;
  if (leftDate != null) {
    const serialDate = excelSerialMilliseconds(right);
    if (serialDate != null) return Math.abs(leftDate - serialDate) < 1;
  }
  if (rightDate != null) {
    const serialDate = excelSerialMilliseconds(left);
    if (serialDate != null) return Math.abs(rightDate - serialDate) < 1;
  }
  return String(left) === String(right);
}

function parseTableIdFormula(formula) {
  const tableId = String(formula || "")
    .trim()
    .replace(/^=/, "")
    .replace(/^"|"$/g, "")
    .toUpperCase();
  return /^(QUEUE_BOARD|BRANCH_DATA)_[A-Z0-9]+$/.test(tableId) ? tableId : null;
}

function parseDefinedValue(formula) {
  return String(formula || "")
    .trim()
    .replace(/^=/, "")
    .replace(/^"|"$/g, "")
    .replace(/""/g, '"');
}

function snapshotModel(snapshot) {
  const rowIdIndex = snapshot.columns.findIndex(
    (column) => String(column).toUpperCase() === "ROW_ID"
  );
  if (rowIdIndex < 0) throw new Error("Server snapshot does not contain ROW_ID.");
  const columns = snapshot.columns
    .filter((_, index) => index !== rowIdIndex)
    .map((column) => String(column).toUpperCase());
  const rows = new Map();
  for (const rawRow of snapshot.rows) {
    const rowId = Number(rawRow[rowIdIndex]);
    const values = {};
    snapshot.columns.forEach((column, index) => {
      if (index !== rowIdIndex) values[String(column).toUpperCase()] = normalizeValue(rawRow[index]);
    });
    rows.set(rowId, values);
  }
  return { columns, rows };
}

function semanticSheet(snapshot) {
  const sheet = snapshot?.semantic?.sheets?.[0];
  if (!sheet) throw new Error("This checkout has no Stage 2 identity map. Pull latest and reconnect.");
  return sheet;
}

function sequenceMoves(baseIds, localIds, positions) {
  const localSet = new Set(localIds);
  const working = baseIds.filter((id) => localSet.has(id));
  const target = localIds.filter((id) => working.includes(id));
  const moves = [];
  target.forEach((id, targetIndex) => {
    const currentIndex = working.indexOf(id);
    if (currentIndex === targetIndex) return;
    working.splice(currentIndex, 1);
    working.splice(targetIndex, 0, id);
    moves.push({ id, position: positions.get(id) });
  });
  return moves;
}

function buildSemanticSheetDiff(baseSheet, workbook) {
  const operations = [];
  const preview = [];
  const baseColumns = [...baseSheet.columns].sort((a, b) => a.position - b.position);
  const unusedBase = new Set(baseColumns.map((column) => column.column_id));
  const mappedColumns = workbook.columns.map((name, position) => {
    let match = baseColumns.find(
      (column) => unusedBase.has(column.column_id) && column.name === name
    );
    if (!match) {
      match = baseColumns.find(
        (column) => unusedBase.has(column.column_id) && column.position === position
      );
    }
    if (match) unusedBase.delete(match.column_id);
    return {
      columnId: match?.column_id || clientStableId("COL"),
      name,
      position,
      base: match || null,
    };
  });
  const columnByName = new Map(mappedColumns.map((column) => [column.name, column]));
  const columnPositions = new Map(mappedColumns.map((column) => [column.columnId, column.position]));

  mappedColumns.forEach((column) => {
    if (!column.base) {
      operations.push({
        operation_type: "COLUMN_INSERT", sheet_id: baseSheet.sheet_id,
        column_id: column.columnId, new_column_position: column.position,
        new_value: column.name, new_data_type: "TEXT",
      });
      preview.push(`COLUMN_INSERT ${column.name} at ${column.position + 1}`);
    } else if (column.base.name !== column.name) {
      operations.push({
        operation_type: "COLUMN_RENAME", sheet_id: baseSheet.sheet_id,
        column_id: column.columnId, old_value: column.base.name, new_value: column.name,
      });
      preview.push(`COLUMN_RENAME ${column.base.name} -> ${column.name}`);
    }
  });
  unusedBase.forEach((columnId) => {
    const column = baseColumns.find((item) => item.column_id === columnId);
    operations.push({
      operation_type: "COLUMN_DELETE", sheet_id: baseSheet.sheet_id,
      column_id: columnId, previous_column_position: column.position, old_value: column.name,
    });
    preview.push(`COLUMN_DELETE ${column.name}`);
  });
  sequenceMoves(
    baseColumns.map((column) => column.column_id),
    mappedColumns.map((column) => column.columnId),
    columnPositions
  ).forEach(({ id, position }) => {
    const base = baseColumns.find((column) => column.column_id === id);
    operations.push({
      operation_type: "COLUMN_MOVE", sheet_id: baseSheet.sheet_id, column_id: id,
      previous_column_position: base.position, new_column_position: position,
    });
    preview.push(`COLUMN_MOVE ${base.name}: ${base.position + 1} -> ${position + 1}`);
  });

  if (baseSheet.name !== workbook.sheetName) {
    operations.push({
      operation_type: "SHEET_RENAME", sheet_id: baseSheet.sheet_id,
      old_value: baseSheet.name, new_value: workbook.sheetName,
    });
    preview.push(`SHEET_RENAME ${baseSheet.name} -> ${workbook.sheetName}`);
  }

  const baseRows = [...baseSheet.rows].sort((a, b) => a.position - b.position);
  const baseById = new Map(baseRows.map((row) => [row.row_id, row]));
  const dateReferenceByColumn = new Map(
    mappedColumns.map((column) => [
      column.columnId,
      baseRows.map((row) => row.values[column.columnId]).find(
        (value) => dateTextMilliseconds(value) != null
      ) || null,
    ])
  );
  const seenRows = new Set();
  const localRows = workbook.rows.map((row, position) => ({
    ...row,
    rowId: row.rowId || clientStableId("ROW"),
    position,
  }));
  const rowPositions = new Map(localRows.map((row) => [row.rowId, row.position]));

  localRows.forEach((row) => {
    if (seenRows.has(row.rowId)) throw new Error(`Duplicate hidden row identity ${row.rowId}. Pull latest to repair it.`);
    seenRows.add(row.rowId);
    const original = baseById.get(row.rowId);
    const valuesById = {};
    mappedColumns.forEach((column) => {
      const reference = original?.values[column.columnId]
        ?? dateReferenceByColumn.get(column.columnId);
      valuesById[column.columnId] = normalizeSemanticValue(
        row.values[column.name] ?? null, reference, column.base?.data_type
      );
    });
    if (!original) {
      operations.push({
        operation_type: "ROW_INSERT", sheet_id: baseSheet.sheet_id,
        row_id: row.rowId, new_row_position: row.position, new_value: valuesById,
      });
      preview.push(`ROW_INSERT at ${row.position + 2}`);
    }
    mappedColumns.forEach((column) => {
      if (!original) {
        if (row.formulas[column.name]) {
          operations.push({
            operation_type: "CELL_FORMULA_UPDATE", sheet_id: baseSheet.sheet_id,
            row_id: row.rowId, column_id: column.columnId,
            old_formula: null, new_formula: row.formulas[column.name],
          });
        }
        return;
      }
      if (!column.base) {
        const newValue = normalizeSemanticValue(
          row.values[column.name] ?? null,
          dateReferenceByColumn.get(column.columnId),
          column.base?.data_type
        );
        if (newValue != null) {
          operations.push({
            operation_type: "CELL_VALUE_UPDATE", sheet_id: baseSheet.sheet_id,
            row_id: row.rowId, column_id: column.columnId,
            old_value: null, new_value: newValue,
          });
          preview.push(`${column.name} [${row.position + 2}]: NULL -> ${String(newValue)}`);
        }
        if (row.formulas[column.name]) {
          operations.push({
            operation_type: "CELL_FORMULA_UPDATE", sheet_id: baseSheet.sheet_id,
            row_id: row.rowId, column_id: column.columnId,
            old_formula: null, new_formula: row.formulas[column.name],
          });
        }
        return;
      }
      const oldValue = original.values[column.columnId] ?? null;
      const newValue = normalizeSemanticValue(
        row.values[column.name] ?? null,
        oldValue,
        column.base?.data_type
      );
      const oldFormula = original.formulas?.[column.columnId] || null;
      const newFormula = row.formulas[column.name] || null;
      if (!valuesEqual(oldValue, newValue)) {
        operations.push({
          operation_type: "CELL_VALUE_UPDATE", sheet_id: baseSheet.sheet_id,
          row_id: row.rowId, column_id: column.columnId,
          old_value: oldValue, new_value: newValue,
        });
        preview.push(`${column.name} [${row.position + 2}]: ${String(oldValue)} -> ${String(newValue)}`);
      }
      if (oldFormula !== newFormula) {
        operations.push({
          operation_type: "CELL_FORMULA_UPDATE", sheet_id: baseSheet.sheet_id,
          row_id: row.rowId, column_id: column.columnId,
          old_formula: oldFormula, new_formula: newFormula,
        });
        preview.push(`FORMULA ${column.name} [${row.position + 2}]: ${oldFormula || "none"} -> ${newFormula || "none"}`);
      }
    });
  });

  sequenceMoves(
    baseRows.map((row) => row.row_id),
    localRows.map((row) => row.rowId),
    rowPositions
  ).forEach(({ id, position }) => {
    const original = baseById.get(id);
    operations.push({
      operation_type: "ROW_MOVE", sheet_id: baseSheet.sheet_id, row_id: id,
      previous_row_position: original.position, new_row_position: position,
    });
    preview.push(`ROW_MOVE ${original.position + 2} -> ${position + 2}`);
  });
  baseRows.filter((row) => !seenRows.has(row.row_id)).forEach((row) => {
    operations.push({
      operation_type: "ROW_DELETE", sheet_id: baseSheet.sheet_id,
      row_id: row.row_id, previous_row_position: row.position,
    });
    preview.push(`ROW_DELETE at ${row.position + 2}`);
  });

  const counts = operations.reduce((result, operation) => {
    if (operation.operation_type.startsWith("CELL_")) result.cells += 1;
    if (operation.operation_type.startsWith("ROW_")) result.rows += 1;
    if (operation.operation_type.startsWith("COLUMN_")) result.columns += 1;
    if (operation.operation_type === "CELL_FORMULA_UPDATE") result.formulas += 1;
    return result;
  }, { cells: 0, rows: 0, columns: 0, formulas: 0 });

  return {
    semantic_changes: operations,
    changeCount: operations.length,
    preview,
    counts,
    updates: [], insert_rows: [], delete_row_ids: [], new_columns: [], delete_columns: [],
  };
}

function buildSemanticWorkbookDiff(baseline, workbook) {
  const baseSheets = [...(baseline?.semantic?.sheets || [])].sort((a, b) => a.position - b.position);
  const localSheets = [...(workbook?.sheets || [])].sort((a, b) => a.sheetPosition - b.sheetPosition);
  const unused = new Set(baseSheets.map((sheet) => sheet.sheet_id));
  const combined = {
    semantic_changes: [], preview: [], changeCount: 0,
    counts: { cells: 0, rows: 0, columns: 0, formulas: 0, sheets: 0 },
    updates: [], insert_rows: [], delete_row_ids: [], new_columns: [], delete_columns: [],
  };
  const mapped = localSheets.map((local, position) => {
    let base = baseSheets.find((sheet) => unused.has(sheet.sheet_id) && sheet.name === local.sheetName);
    if (!base) base = baseSheets.find((sheet) => unused.has(sheet.sheet_id) && sheet.position === position);
    if (base) unused.delete(base.sheet_id);
    if (!base) {
      const sheetId = clientStableId("SHEET");
      combined.semantic_changes.push({
        operation_type: "SHEET_CREATE", sheet_id: sheetId,
        new_value: local.sheetName, new_row_position: position,
      });
      combined.preview.push(`SHEET_CREATE ${local.sheetName}`);
      combined.counts.sheets += 1;
      base = { sheet_id: sheetId, name: local.sheetName, position, columns: [], rows: [] };
    }
    const diff = buildSemanticSheetDiff(base, local);
    combined.semantic_changes.push(...diff.semantic_changes);
    combined.preview.push(...diff.preview.map((line) => `${local.sheetName}: ${line}`));
    Object.keys(diff.counts).forEach((key) => { combined.counts[key] += diff.counts[key] || 0; });
    if (base.position !== position && (baseline?.semantic?.sheets || []).some((sheet) => sheet.sheet_id === base.sheet_id)) {
      combined.semantic_changes.push({
        operation_type: "SHEET_MOVE", sheet_id: base.sheet_id,
        previous_row_position: base.position, new_row_position: position,
      });
      combined.preview.push(`SHEET_MOVE ${local.sheetName}: ${base.position + 1} -> ${position + 1}`);
      combined.counts.sheets += 1;
    }
    return base.sheet_id;
  });
  void mapped;
  unused.forEach((sheetId) => {
    const sheet = baseSheets.find((item) => item.sheet_id === sheetId);
    combined.semantic_changes.push({
      operation_type: "SHEET_DELETE", sheet_id: sheetId,
      old_value: sheet.name, previous_row_position: sheet.position,
    });
    combined.preview.push(`SHEET_DELETE ${sheet.name}`);
    combined.counts.sheets += 1;
  });
  combined.changeCount = combined.semantic_changes.length;
  return combined;
}

function semanticTarget(change) {
  if (change.operation_type.startsWith("CELL_")) return `CELL:${change.row_id}:${change.column_id}`;
  if (change.operation_type.startsWith("ROW_")) return `ROW:${change.row_id}`;
  if (change.operation_type.startsWith("COLUMN_")) return `COLUMN:${change.column_id}`;
  return `SHEET:${change.sheet_id}`;
}

function workbookFromSemantic(snapshot) {
  return {
    sheets: [...(snapshot?.semantic?.sheets || [])].sort((a, b) => a.position - b.position).map((sheet) => {
      const columns = [...sheet.columns].sort((a, b) => a.position - b.position);
      return {
        sheetName: sheet.name,
        sheetPosition: sheet.position,
        columns: columns.map((column) => column.name),
        rows: [...sheet.rows].sort((a, b) => a.position - b.position).map((row) => ({
          rowId: row.row_id,
          values: Object.fromEntries(columns.map((column) => [column.name, row.values[column.column_id] ?? null])),
          formulas: Object.fromEntries(columns.map((column) => [column.name, row.formulas?.[column.column_id] || null])),
        })),
      };
    }),
  };
}

function semanticRebaseConflicts(baseline, remote, localDiff) {
  const remoteDiff = buildSemanticWorkbookDiff(baseline, workbookFromSemantic(remote));
  const remoteTargets = new Set(remoteDiff.semantic_changes.map(semanticTarget));
  return localDiff.semantic_changes
    .filter((change) => remoteTargets.has(semanticTarget(change)))
    .map((change) => `${semanticTarget(change)} changed locally and remotely.`);
}

function applySemanticChanges(snapshot, changes) {
  const rebased = JSON.parse(JSON.stringify(snapshot));
  const sheets = rebased.semantic.sheets;
  const sortState = (sheet) => {
    sheet.columns.sort((a, b) => a.position - b.position)
      .forEach((column, index) => { column.position = index; });
    sheet.rows.sort((a, b) => a.position - b.position)
      .forEach((row, index) => { row.position = index; });
    sheets.sort((a, b) => a.position - b.position)
      .forEach((item, index) => { item.position = index; });
  };
  changes.forEach((change) => {
    const operation = change.operation_type;
    if (operation === "SHEET_CREATE") {
      sheets.splice(change.new_row_position, 0, {
        sheet_id: change.sheet_id, name: change.new_value,
        position: change.new_row_position, columns: [], rows: [],
      });
      sortState(sheets[change.new_row_position]);
      return;
    }
    const sheet = sheets.find((item) => item.sheet_id === change.sheet_id);
    if (!sheet) return;
    if (operation === "SHEET_DELETE") {
      const index = sheets.findIndex((item) => item.sheet_id === change.sheet_id);
      if (index >= 0) sheets.splice(index, 1);
      return;
    }
    if (operation === "SHEET_RENAME") sheet.name = change.new_value;
    if (operation === "SHEET_MOVE") {
      const index = sheets.findIndex((item) => item.sheet_id === change.sheet_id);
      if (index >= 0) sheets.splice(change.new_row_position, 0, sheets.splice(index, 1)[0]);
    }
    if (operation === "COLUMN_INSERT") {
      sheet.columns.splice(change.new_column_position, 0, {
        column_id: change.column_id, name: change.new_value,
        position: change.new_column_position, data_type: change.new_data_type || "TEXT",
      });
      sheet.rows.forEach((row) => { row.values[change.column_id] = null; });
    }
    if (operation === "COLUMN_DELETE") {
      sheet.columns = sheet.columns.filter((column) => column.column_id !== change.column_id);
      sheet.rows.forEach((row) => {
        delete row.values[change.column_id];
        delete row.formulas?.[change.column_id];
      });
    }
    if (operation === "COLUMN_RENAME") {
      const column = sheet.columns.find((item) => item.column_id === change.column_id);
      if (column) column.name = change.new_value;
    }
    if (operation === "COLUMN_MOVE") {
      const index = sheet.columns.findIndex((column) => column.column_id === change.column_id);
      if (index >= 0) sheet.columns.splice(change.new_column_position, 0, sheet.columns.splice(index, 1)[0]);
    }
    if (operation === "ROW_INSERT") {
      sheet.rows.splice(change.new_row_position, 0, {
        row_id: change.row_id, position: change.new_row_position,
        values: { ...(change.new_value || {}) }, formulas: {}, styles: {}, comments: {},
      });
    }
    if (operation === "ROW_DELETE") {
      sheet.rows = sheet.rows.filter((row) => row.row_id !== change.row_id);
    }
    if (operation === "ROW_MOVE") {
      const index = sheet.rows.findIndex((row) => row.row_id === change.row_id);
      if (index >= 0) sheet.rows.splice(change.new_row_position, 0, sheet.rows.splice(index, 1)[0]);
    }
    if (operation.startsWith("CELL_")) {
      const row = sheet.rows.find((item) => item.row_id === change.row_id);
      if (!row) return;
      if (operation === "CELL_VALUE_UPDATE") row.values[change.column_id] = change.new_value;
      if (operation === "CELL_FORMULA_UPDATE") {
        row.formulas ||= {};
        if (change.new_formula) row.formulas[change.column_id] = change.new_formula;
        else delete row.formulas[change.column_id];
      }
    }
    sortState(sheet);
  });
  return rebased;
}

function buildWorkbookDiff(baseline, workbook) {
  const server = snapshotModel(baseline);
  const localColumns = workbook.columns;
  const newColumns = localColumns.filter((column) => !server.columns.includes(column));
  const deleteColumns = server.columns.filter((column) => !localColumns.includes(column));
  const localIds = new Set();
  const updates = [];
  const insertRows = [];
  const preview = [];

  for (const row of workbook.rows) {
    if (row.rowId != null) {
      if (localIds.has(row.rowId)) throw new Error(`Duplicate hidden ROW_ID ${row.rowId}. Pull latest to repair it.`);
      localIds.add(row.rowId);
      const original = server.rows.get(row.rowId);
      if (!original) throw new Error(`ROW_ID ${row.rowId} is not present in base version ${baseline.version}.`);
      for (const column of localColumns) {
        const oldValue = original[column] ?? null;
        const newValue = row.values[column] ?? null;
        if (!valuesEqual(oldValue, newValue)) {
          updates.push({ row_id: row.rowId, column_name: column, new_value: newValue });
          preview.push(`${column} [${row.rowId}]: ${String(oldValue)} -> ${String(newValue)}`);
        }
      }
    } else {
      insertRows.push({ ...row.values });
      preview.push(`New row: ${localColumns.map((column) => `${column}=${row.values[column] ?? "NULL"}`).join(", ")}`);
    }
  }

  const deleteRowIds = [...server.rows.keys()].filter((rowId) => !localIds.has(rowId));
  deleteRowIds.forEach((rowId) => preview.push(`Deleted row: ROW_ID=${rowId}`));
  newColumns.forEach((column) => preview.push(`Added column: ${column}`));
  deleteColumns.forEach((column) => preview.push(`Deleted column: ${column}`));

  const changeCount = updates.length + insertRows.length + deleteRowIds.length
    + newColumns.length + deleteColumns.length;
  return {
    updates,
    insert_rows: insertRows,
    delete_row_ids: deleteRowIds,
    new_columns: newColumns,
    delete_columns: deleteColumns,
    changeCount,
    preview,
  };
}

function rowChanged(columns, before, after) {
  return columns.some((column) => !valuesEqual(before?.[column], after?.[column]));
}

function findRebaseConflicts(baseline, remote, diff) {
  const base = snapshotModel(baseline);
  const latest = snapshotModel(remote);
  const conflicts = [];
  for (const update of diff.updates) {
    const before = base.rows.get(update.row_id);
    const after = latest.rows.get(update.row_id);
    if (!after) {
      conflicts.push(`ROW_ID ${update.row_id} was deleted on the server.`);
      continue;
    }
    const remoteValue = after[update.column_name] ?? null;
    const baseValue = before?.[update.column_name] ?? null;
    if (!valuesEqual(baseValue, remoteValue) && !valuesEqual(remoteValue, update.new_value)) {
      conflicts.push(`${update.column_name} [${update.row_id}] changed both locally and remotely.`);
    }
  }
  for (const rowId of diff.delete_row_ids) {
    const before = base.rows.get(rowId);
    const after = latest.rows.get(rowId);
    if (after && rowChanged(base.columns, before, after)) {
      conflicts.push(`ROW_ID ${rowId} was edited remotely but deleted locally.`);
    }
  }
  for (const column of diff.new_columns) {
    if (!base.columns.includes(column) && latest.columns.includes(column)) {
      conflicts.push(`Column ${column} was also added remotely.`);
    }
  }
  for (const column of diff.delete_columns) {
    if (!latest.columns.includes(column)) continue;
    for (const [rowId, before] of base.rows) {
      const after = latest.rows.get(rowId);
      if (after && !valuesEqual(before[column], after[column])) {
        conflicts.push(`Column ${column} has remote edits and cannot be deleted automatically.`);
        break;
      }
    }
  }
  return conflicts;
}

function rebaseDiffOntoSnapshot(remote, diff) {
  const latest = snapshotModel(remote);
  let columns = [...latest.columns];
  for (const column of diff.new_columns) if (!columns.includes(column)) columns.push(column);
  columns = columns.filter((column) => !diff.delete_columns.includes(column));

  const rows = new Map();
  for (const [rowId, values] of latest.rows) rows.set(rowId, { ...values });
  for (const update of diff.updates) {
    if (rows.has(update.row_id)) rows.get(update.row_id)[update.column_name] = update.new_value;
  }
  diff.delete_row_ids.forEach((rowId) => rows.delete(rowId));

  const snapshotRows = [...rows.entries()].map(([rowId, values]) => [
    rowId,
    ...columns.map((column) => values[column] ?? null),
  ]);
  for (const values of diff.insert_rows) {
    snapshotRows.push([null, ...columns.map((column) => values[column] ?? null)]);
  }
  return {
    ...remote,
    columns: ["ROW_ID", ...columns],
    rows: snapshotRows,
    total: snapshotRows.length,
  };
}

const containerStyle = {
  minHeight: "100vh", padding: "18px 15px", color: "#e8f0f2",
  background: "linear-gradient(180deg,#181818 0,#1f1f1f 100%)",
  display: "flex", flexDirection: "column", gap: 13,
};
const cardStyle = {
  padding: 14, border: "1px solid #3c3c3c", borderRadius: 6,
  background: "#252526", boxShadow: "0 8px 24px rgba(0,0,0,.16)",
};
const labelStyle = {
  color: "#8399a5", fontSize: 10, fontWeight: 800, letterSpacing: ".12em",
  textTransform: "uppercase", marginBottom: 7,
};
const inputStyle = {
  boxSizing: "border-box", width: "100%", padding: "10px 11px", color: "#edf8f7",
  background: "#1e1e1e", border: "1px solid #4c4c4c", borderRadius: 4, outline: "none",
};
const buttonStyle = {
  width: "100%", padding: "11px 13px", border: "1px solid #2ea043", borderRadius: 4, color: "#fff",
  background: "#238636", fontSize: 12, fontWeight: 800,
  cursor: "pointer", boxShadow: "0 4px 14px rgba(35,134,54,.18)",
};

async function purgeWorkbookData(lockNotice = "[LOCKED] Verification required to display branch data.") {
  if (typeof window === "undefined" || !window.Excel || !window.Excel.run) return;
  try {
    await window.Excel.run(async (context) => {
      const worksheets = context.workbook.worksheets;
      worksheets.load("items/id,name,position");
      await context.sync();

      for (const sheet of worksheets.items) {
        const used = sheet.getUsedRangeOrNullObject(true);
        used.load(["isNullObject", "rowIndex", "columnIndex", "rowCount", "columnCount"]);
        const tables = sheet.tables;
        tables.load("items");
        await context.sync();

        if (used.isNullObject) continue;

        // Process Excel tables on this worksheet
        if (tables.items && tables.items.length > 0) {
          for (const table of tables.items) {
            try {
              const tableRange = table.getRange();
              tableRange.load(["rowIndex", "columnIndex", "rowCount", "columnCount"]);
              const dataBody = table.getDataBodyRangeOrNullObject();
              dataBody.load(["isNullObject"]);
              await context.sync();

              // Clear all existing data rows/formulas inside table
              if (!dataBody.isNullObject) {
                dataBody.clear(window.Excel.ClearApplyTo.contents);
              }

              const startRow = tableRange.rowIndex;
              const startCol = tableRange.columnIndex;
              const numCols = Math.max(1, tableRange.columnCount);
              const oldRows = tableRange.rowCount;

              if (oldRows > 1) {
                // Resize table down to header + 1 placeholder row (2 rows total)
                const minTarget = sheet.getRangeByIndexes(startRow, startCol, 2, numCols);
                table.resize(minTarget);
                await context.sync();

                // Clear any leftover cells below the resized table
                if (oldRows > 2) {
                  sheet
                    .getRangeByIndexes(startRow + 2, startCol, oldRows - 2, numCols)
                    .clear(window.Excel.ClearApplyTo.all);
                }

                // Place the lockNotice in column 0 of row 2 (startRow + 1)
                const placeholder = Array.from({ length: numCols }, (_, idx) => (idx === 0 ? lockNotice : ""));
                const rowRange = sheet.getRangeByIndexes(startRow + 1, startCol, 1, numCols);
                rowRange.values = [placeholder];
              }
            } catch (tblErr) {
              console.warn("Table wipe warning:", tblErr);
            }
          }

          // Clear any extra rows below row 2 across the used range
          if (used.rowCount > 2) {
            try {
              sheet
                .getRangeByIndexes(used.rowIndex + 2, used.columnIndex, used.rowCount - 2, used.columnCount)
                .clear(window.Excel.ClearApplyTo.all);
            } catch {
              // ignore
            }
          }
        } else {
          // Plain sheet without Excel tables:
          if (used.rowCount > 1) {
            // Clear all data rows below header row (row 1 onwards)
            sheet
              .getRangeByIndexes(used.rowIndex + 1, used.columnIndex, used.rowCount - 1, used.columnCount)
              .clear(window.Excel.ClearApplyTo.all);

            // Put locked notice in the first cell under header
            sheet.getCell(used.rowIndex + 1, used.columnIndex).values = [[lockNotice]];
          }
        }
      }
      try {
        if (context.workbook && typeof context.workbook.save === "function") {
          context.workbook.save(window.Excel?.SaveBehavior?.save || "Save");
        }
      } catch {
        // ignore save errors
      }
      await context.sync();
    });
  } catch (err) {
    console.warn("Could not purge workbook data:", err);
  }
}

function PathBlockedPanel({ expectedPath, currentPath, onWipeData }) {
  const [wiped, setWiped] = useState(true);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    purgeWorkbookData("[LOCKED] Unauthorized file location. Verification blocked.");
  }, []);

  const handleWipe = async () => {
    setBusy(true);
    try {
      if (onWipeData) {
        await onWipeData();
      } else {
        await purgeWorkbookData("[LOCKED] Unauthorized file location. Verification blocked.");
      }
      setWiped(true);
    } catch {
      // ignore
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={containerStyle}>
      <div style={{ marginTop: 25 }}>
        <div style={{ color: "#f85149", fontSize: 10, fontWeight: 800, letterSpacing: ".14em" }}>
          SECURITY ACCESS BLOCKED
        </div>
        <h1 style={{ margin: "8px 0", fontSize: 23, color: "#ff8b82" }}>Unauthorized Location</h1>
        <p style={{ color: "#c9d1d9", fontSize: 11, lineHeight: 1.6 }}>
          This workbook cannot load server data because its current file location does not match the authorized path injected during download.
        </p>
      </div>

      <div style={{ ...cardStyle, borderColor: "#2ea043", background: "rgba(46,160,67,0.12)", marginTop: 14, padding: "10px 12px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6, color: "#75ead0", fontSize: 11, fontWeight: 700 }}>
          <span>✓</span> Data Rows Wiped & Protected
        </div>
        <div style={{ color: "#8fa4af", fontSize: 10, marginTop: 4, lineHeight: 1.5 }}>
          All worksheet data has been purged from this unauthorized copy. Sensitive EUC data is protected from exposure.
        </div>
      </div>

      <div style={{ ...cardStyle, borderColor: "#f85149", background: "rgba(248,81,73,0.08)", marginTop: 12 }}>
        <div style={{ ...labelStyle, color: "#ff8b82" }}>Authorized Download Location</div>
        <div style={{ color: "#75ead0", fontSize: 10, fontFamily: "Consolas, monospace", wordBreak: "break-all", marginBottom: 12 }}>
          {expectedPath || "Not specified"}
        </div>

        <div style={{ ...labelStyle, color: "#ff8b82" }}>Current Open Location</div>
        <div style={{ color: "#ffaaa3", fontSize: 10, fontFamily: "Consolas, monospace", wordBreak: "break-all" }}>
          {currentPath || "Copied / moved outside authorized directory"}
        </div>
      </div>

      <div style={{ ...cardStyle, marginTop: 12 }}>
        <div style={{ ...labelStyle, color: "#e3b341" }}>Copy & Leak Protection</div>
        <p style={{ color: "#8fa4af", fontSize: 11, lineHeight: 1.6, margin: 0 }}>
          To prevent unauthorized copies and leakage of EUC data, this workbook is cryptographically locked to its registered download folder. Please open the workbook from:
        </p>
        <div style={{ color: "#58a6ff", fontSize: 10, fontFamily: "Consolas, monospace", marginTop: 8, wordBreak: "break-all" }}>
          {expectedPath}
        </div>
        <button
          type="button"
          style={{ ...buttonStyle, background: "#da3633", borderColor: "#f85149", marginTop: 14 }}
          onClick={handleWipe}
          disabled={busy}
        >
          {busy ? "Purging data..." : wiped ? "✓ Re-purge Worksheet Data" : "Purge Worksheet Data"}
        </button>
      </div>
    </div>
  );
}

function WorkbookVerificationPanel({ embeddedInfo, onVerifiedAndLoaded }) {
  const assignedEmail = embeddedInfo.assigned_email || "";
  const [email, setEmail] = useState(assignedEmail);
  const [code, setCode] = useState("");
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  const submit = async (e) => {
    if (e) e.preventDefault();
    setError("");
    setBusy(true);
    try {
      if (!sent) {
        const result = await requestLoginCode(email.trim());
        setSent(true);
        setNote(result.dev_otp ? `Local code: ${result.dev_otp}` : "Check your email for the 6-digit code.");
        if (result.dev_otp) setCode(result.dev_otp);
      } else {
        const currentDocPath = (typeof Office !== "undefined" && Office?.context?.document?.url)
          ? Office.context.document.url
          : (embeddedInfo.local_file_path || "");
        const result = await verifyWorkbookAccess({
          repository_id: embeddedInfo.repository_id,
          branch_id: embeddedInfo.branch_id,
          working_copy_id: embeddedInfo.working_copy_id,
          email: email.trim(),
          code: code.trim(),
          current_file_path: currentDocPath,
          machine_id: getDeviceId(),
        });
        await onVerifiedAndLoaded(result);
      }
    } catch (err) {
      if (err.detail?.code === "DEVICE_BLOCKED") {
        await purgeWorkbookData("[LOCKED] This device was blocked by the repository owner.");
        setError("This device has been blocked by the repository owner. Contact them to restore access.");
      } else if (err.detail?.code === "DEVICE_MISMATCH") {
        await purgeWorkbookData("[LOCKED] This workbook is locked to a different device. Ask the repository owner to trust this device.");
        setError("This workbook was first opened on a different device and is locked to it. Ask the repository owner to trust this device from Team Activity, then try again.");
      } else if (err.message?.includes("ASSIGNED_USER_MISMATCH")) {
        setError("This workbook is issued to a different account. Ask the repository owner to issue you your own branch workbook.");
      } else {
        setError(err.message || "Verification failed. Check your code and role.");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={containerStyle}>
      <div style={{ marginTop: 20 }}>
        <div style={{ color: "#58a6ff", fontSize: 10, fontWeight: 800, letterSpacing: ".14em" }}>
          GIT WALK / ACCESS VERIFICATION
        </div>
        <h1 style={{ margin: "6px 0", fontSize: 22 }}>Verify Identity & Role</h1>
        <p style={{ color: "#8fa4af", fontSize: 11, lineHeight: 1.5 }}>
          Authorized location verified. Authenticate via verification code to confirm your role and load branch data.
        </p>
      </div>

      <div style={{ ...cardStyle, background: "#15222e", borderColor: "#1f4a6e", marginBottom: 12, padding: 10 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6, color: "#75ead0", fontSize: 10, fontWeight: 700 }}>
          <span>✓</span> Path Verified on Authorized Drive
        </div>
        <div style={{ color: "#8fa4af", fontSize: 9, marginTop: 4, wordBreak: "break-all", fontFamily: "Consolas, monospace" }}>
          {embeddedInfo.local_file_path}
        </div>
        <div style={{ marginTop: 6, display: "flex", justifyContent: "space-between", fontSize: 10, color: "#8fa4af" }}>
          <span>Branch: <strong style={{ color: "#fff" }}>{embeddedInfo.branch_name || embeddedInfo.branch_id}</strong></span>
        </div>
      </div>

      <form onSubmit={submit} style={{ ...cardStyle, display: "grid", gap: 11 }}>
        <div>
          <div style={labelStyle}>
            {assignedEmail ? (
              <span style={{ display: "flex", alignItems: "center", gap: 5 }}>
                <span>🔒</span> Issued to
              </span>
            ) : (
              "Work email"
            )}
          </div>
          <input
            style={{ ...inputStyle, ...(assignedEmail ? { opacity: 0.75, cursor: "not-allowed" } : {}) }}
            type="email"
            value={email}
            onChange={(e) => { if (!assignedEmail) setEmail(e.target.value); }}
            placeholder="name@company.com"
            disabled={sent || busy}
            readOnly={!!assignedEmail}
            autoFocus
          />
          {assignedEmail ? (
            <div style={{ color: "#8fa4af", fontSize: 10, marginTop: 4 }}>
              This workbook is issued to {assignedEmail}. Sign in with another account isn't allowed on this file.
            </div>
          ) : null}
        </div>
        {sent ? (
          <div>
            <div style={labelStyle}>6-digit verification code</div>
            <input
              style={inputStyle}
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
              placeholder="123456"
              disabled={busy}
              autoFocus
            />
          </div>
        ) : null}
        {note ? <div style={{ color: "#75ead0", fontSize: 11 }}>{note}</div> : null}
        {error ? (
          <div style={{ color: "#ff8b82", fontSize: 11, background: "rgba(248,81,73,0.1)", border: "1px solid rgba(248,81,73,0.3)", padding: "8px 10px", borderRadius: 4, lineHeight: 1.4 }}>
            {error}
          </div>
        ) : null}
        <button
          style={{ ...buttonStyle, marginTop: 4 }}
          disabled={busy || !email.trim() || (sent && code.length !== 6)}
          type="submit"
        >
          {busy ? "Working..." : sent ? "Verify & Load Data" : "Email me a code"}
        </button>
        {sent ? (
          <button
            type="button"
            style={{ background: "transparent", border: 0, color: "#58a6ff", fontSize: 11, cursor: "pointer", textAlign: "center", padding: "4px 0" }}
            onClick={() => { setSent(false); setCode(""); setError(""); setNote(""); }}
          >
            Use a different email
          </button>
        ) : null}
      </form>
    </div>
  );
}

function AuthPanel({ onAuthenticated }) {
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  const submit = async () => {
    setBusy(true); setError("");
    try {
      if (!sent) {
        const result = await requestLoginCode(email.trim());
        setSent(true);
        setNote(result.dev_otp ? `Local code: ${result.dev_otp}` : "Check your email for the 6-digit code.");
        if (result.dev_otp) setCode(result.dev_otp);
      } else {
        onAuthenticated(await verifyLoginCode(email.trim(), code.trim(), getDeviceId()));
      }
    } catch (err) { setError(err.message); } finally { setBusy(false); }
  };

  return (
    <div style={containerStyle}>
      <div style={{ marginTop: 25 }}>
        <div style={{ color: "#58a6ff", fontSize: 10, fontWeight: 800, letterSpacing: ".14em" }}>GIT WALK FOR EXCEL</div>
        <h1 style={{ margin: "8px 0", fontSize: 27 }}>Sign in to sync</h1>
        <p style={{ color: "#8fa4af", fontSize: 12, lineHeight: 1.6 }}>Clone, commit, pull, and audit Excel data safely.</p>
      </div>
      <div style={{ ...cardStyle, display: "grid", gap: 11 }}>
        <div><div style={labelStyle}>Work email</div><input style={inputStyle} type="email" value={email} onChange={(e) => setEmail(e.target.value)} disabled={sent} /></div>
        {sent ? <div><div style={labelStyle}>Verification code</div><input style={inputStyle} value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))} /></div> : null}
        {note ? <div style={{ color: "#75ead0", fontSize: 11 }}>{note}</div> : null}
        {error ? <div style={{ color: "#ff8b82", fontSize: 11 }}>{error}</div> : null}
        <button style={buttonStyle} disabled={busy || !email || (sent && code.length !== 6)} onClick={submit}>{busy ? "Working..." : sent ? "Verify and continue" : "Email me a code"}</button>
      </div>
    </div>
  );
}

export default function TaskpaneUI() {
  const [auth, setAuth] = useState(getStoredAuth());
  const [authBootstrapping, setAuthBootstrapping] = useState(true);
  const [embeddedInfo, setEmbeddedInfo] = useState(null);
  const [pathBlocked, setPathBlocked] = useState(false);
  const [pathDetails, setPathDetails] = useState({ expected: "", current: "" });
  const [workbookVerified, setWorkbookVerified] = useState(false);
  const [tableId, setTableId] = useState("");
  const [connected, setConnected] = useState(false);
  const [syncStatus, setSyncStatus] = useState("idle");
  const [statusMsg, setStatusMsg] = useState(null);
  const [logs, setLogs] = useState([]);
  const [baseVersion, setBaseVersion] = useState(0);
  const [dirty, setDirty] = useState(false);
  const [staged, setStaged] = useState(null);
  const [commitMessage, setCommitMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [conflicts, setConflicts] = useState([]);
  const [divergence, setDivergence] = useState(null);
  const [workspaceClosed, setWorkspaceClosed] = useState(false);
  const [formulaExplainBusy, setFormulaExplainBusy] = useState(false);
  const [formulaExplainRunId, setFormulaExplainRunId] = useState(null);
  const [formulaExplainSteps, setFormulaExplainSteps] = useState([]);
  const [formulaExplainResult, setFormulaExplainResult] = useState(null);
  const [branchName, setBranchName] = useState("");
  const [pullSource, setPullSource] = useState("branch");
  const [lastCommitId, setLastCommitId] = useState(null);
  const [recoveredDraft, setRecoveredDraft] = useState(null);
  const [pendingCommit, setPendingCommit] = useState(null);
  const [pendingCommitBusy, setPendingCommitBusy] = useState(false);
  const [deviceBlocked, setDeviceBlocked] = useState(false);
  const [workStatus, setWorkStatus] = useState("ONLINE");

  const baselineRef = useRef(null);
  const workbookIdentityRef = useRef(null);
  const handlerRef = useRef([]);
  const applyingRemoteRef = useRef(false);
  const autoConnectedRef = useRef(false);
  const autoAuthAttemptedRef = useRef(false);
  const displayHeadersRef = useRef({});
  const logEndRef = useRef(null);
  const presenceTimerRef = useRef(null);
  const clientIdRef = useRef(getClientId("excel"));
  const workStatusRef = useRef("ONLINE");
  const deviceBlockedRef = useRef(false);
  const retryPendingCommitRef = useRef(null);
  const reviewChangesRef = useRef(null);

  useEffect(() => {
    const expireSession = () => setAuth(null);
    window.addEventListener("gitwalk:auth-expired", expireSession);
    return () => window.removeEventListener("gitwalk:auth-expired", expireSession);
  }, []);

  useEffect(() => { logEndRef.current?.scrollIntoView({ behavior: "smooth" }); }, [logs]);
  const addLog = useCallback((message, isError = false) => {
    const time = new Date().toLocaleTimeString();
    setLogs((previous) => [...previous.slice(-119), { time, message, isError }]);
  }, []);

  useEffect(() => {
    const onReconnect = () => {
      if (!pendingCommit) return;
      addLog("Network connection restored. Retrying pending commit automatically...");
      retryPendingCommitRef.current?.();
    };
    window.addEventListener("online", onReconnect);
    return () => window.removeEventListener("online", onReconnect);
  }, [addLog, pendingCommit]);

  const saveBaseVersion = useCallback((version, targetTableId = tableId, headCommitId = null) => new Promise((resolve) => {
    Office.context.document.settings.set(`baseVersion:${targetTableId}`, version);
    Office.context.document.settings.set("baseVersion", version);
    if (headCommitId) Office.context.document.settings.set(`baseHead:${targetTableId}`, headCommitId);
    Office.context.document.settings.saveAsync(() => resolve());
  }), [tableId]);

  // Local, no-network draft/pending-commit persistence — stored in the
  // workbook file itself (Office.context.document.settings), so it survives
  // an Excel crash/close-without-committing and a failed commit request,
  // without needing a server round-trip to save.
  const saveDraft = useCallback((targetTableId, draft) => new Promise((resolve) => {
    Office.context.document.settings.set(`draftDiff:${targetTableId}`, JSON.stringify(draft));
    Office.context.document.settings.saveAsync(() => resolve());
  }), []);
  const loadDraft = useCallback((targetTableId) => {
    const raw = Office.context.document.settings.get(`draftDiff:${targetTableId}`);
    if (!raw) return null;
    try { return JSON.parse(raw); } catch { return null; }
  }, []);
  const clearDraft = useCallback((targetTableId) => new Promise((resolve) => {
    Office.context.document.settings.remove(`draftDiff:${targetTableId}`);
    Office.context.document.settings.saveAsync(() => resolve());
  }), []);

  const savePendingCommit = useCallback((targetTableId, payload) => new Promise((resolve) => {
    Office.context.document.settings.set(`pendingCommit:${targetTableId}`, JSON.stringify({ ...payload, saved_at: new Date().toISOString() }));
    Office.context.document.settings.saveAsync(() => resolve());
  }), []);
  const loadPendingCommit = useCallback((targetTableId) => {
    const raw = Office.context.document.settings.get(`pendingCommit:${targetTableId}`);
    if (!raw) return null;
    try { return JSON.parse(raw); } catch { return null; }
  }, []);
  const clearPendingCommit = useCallback((targetTableId) => new Promise((resolve) => {
    Office.context.document.settings.remove(`pendingCommit:${targetTableId}`);
    Office.context.document.settings.saveAsync(() => resolve());
  }), []);

  // Shared reaction to a DEVICE_BLOCKED response from any endpoint (login,
  // workbook-verify, commit, or the periodic presence heartbeat below):
  // protect whatever is currently staged-but-uncommitted BEFORE wiping the
  // sheet, so a block never silently destroys local work.
  const handleDeviceBlocked = useCallback(async (targetTableId = tableId) => {
    // Idempotency guard: purging the sheet fires Excel onChanged events,
    // which drive markDirty -> another heartbeat -> another 403 -> another
    // call here. A synchronous ref (not state, which updates async) stops
    // that from looping.
    if (deviceBlockedRef.current) return;
    deviceBlockedRef.current = true;
    clearInterval(presenceTimerRef.current);
    setDeviceBlocked(true);
    // Force-compute the current diff first: `staged` can be null/stale if
    // the user made raw cell edits but never triggered reviewChanges() —
    // without this, those edits would never be captured before the purge.
    let latestStaged = staged;
    if (targetTableId && baselineRef.current) {
      try {
        latestStaged = await reviewChangesRef.current?.();
      } catch {
        // Excel may already be unreachable; fall back to whatever was staged.
      }
    }
    if (targetTableId && latestStaged?.changeCount) {
      await saveDraft(targetTableId, {
        base_head: baselineRef.current?.head_commit_id, base_version: baselineRef.current?.version,
        semantic_changes: latestStaged.semantic_changes, commit_message: commitMessage,
        blocked_at: new Date().toISOString(),
      }).catch(() => {});
      addLog(`Saved ${latestStaged.changeCount} uncommitted change(s) locally before locking — they'll be offered for recovery once access is restored.`, true);
    }
    await purgeWorkbookData("[LOCKED] This device was blocked by the repository owner. Any unsaved changes were saved locally.");
    setSyncStatus("error"); setStatusMsg("Device blocked by repository owner");
    addLog("This device has been blocked by the repository owner. Contact them to restore access.", true);
  }, [addLog, commitMessage, saveDraft, staged, tableId]);

  const readWorkbook = useCallback(async () => window.Excel.run(async (context) => {
    const worksheets = context.workbook.worksheets;
    worksheets.load("items/id,name,position");
    await context.sync();
    const entries = worksheets.items.map((sheet) => {
      const used = sheet.getUsedRangeOrNullObject(true);
      used.load(["isNullObject", "values", "formulas", "rowCount", "columnCount"]);
      return { sheet, used };
    });
    await context.sync();
    const sheets = entries.map(({ sheet, used }) => {
      if (used.isNullObject) {
        return { sheetId: sheet.id, sheetName: sheet.name, sheetPosition: sheet.position, columns: [], rows: [] };
      }
      const headers = used.values[0] || [];
      const metadataIndex = headers.findIndex((value) => {
        const header = String(value).trim();
        return header === ROW_ID_HEADER || header === LEGACY_ROW_ID_HEADER;
      });
      const columns = [];
      const headerIndexes = [];
      const seen = new Set();
      headers.forEach((raw, index) => {
        if (index === metadataIndex || String(raw || "").trim() === "") return;
        const column = sanitizeColumnName(raw);
        if (seen.has(column)) throw new Error(`Duplicate column after sanitizing on ${sheet.name}: ${column}`);
        seen.add(column); columns.push(column); headerIndexes.push(index);
        displayHeadersRef.current[`${sheet.name}:${column}`] = String(raw);
      });
      const rows = [];
      for (let rowIndex = 1; rowIndex < used.rowCount; rowIndex += 1) {
        const rawId = metadataIndex >= 0 ? used.values[rowIndex]?.[metadataIndex] : null;
        const rowId = rawId === "" || rawId == null ? null : String(rawId).trim().toUpperCase();
        if (rowId != null && !/^ROW_[A-Z0-9]+$/.test(rowId)) {
          throw new Error(`Invalid hidden Git Walk row identity on ${sheet.name}, row ${rowIndex + 1}. Pull latest to repair it.`);
        }
        const values = {};
        const formulas = {};
        headerIndexes.forEach((columnIndex, index) => {
          values[columns[index]] = normalizeValue(used.values[rowIndex]?.[columnIndex]);
          const formula = used.formulas[rowIndex]?.[columnIndex];
          formulas[columns[index]] = typeof formula === "string" && formula.startsWith("=") ? formula : null;
        });
        const hasData = columns.some((column) => values[column] != null);
        if (hasData || rowId != null) rows.push({ rowId, values, formulas });
      }
      return { sheetId: sheet.id, sheetName: sheet.name, sheetPosition: sheet.position, columns, rows };
    });
    return { sheets };
  }), []);

  const ensureRowIdentity = useCallback(async (snapshot) => window.Excel.run(async (context) => {
    const worksheets = context.workbook.worksheets;
    worksheets.load("items/id,name,position");
    await context.sync();
    const semanticSheets = [...(snapshot?.semantic?.sheets || [])].sort((a, b) => a.position - b.position);
    for (const semantic of semanticSheets) {
      const sheet = worksheets.items.find((item) => item.name === semantic.name)
        || worksheets.items.find((item) => item.position === semantic.position);
      if (!sheet) continue;
      const used = sheet.getUsedRangeOrNullObject(true);
      used.load(["isNullObject", "values", "rowCount", "columnCount"]);
      const tables = sheet.tables;
      tables.load("items");
      await context.sync();
      if (used.isNullObject) continue;
      const expectedIds = [...semantic.rows].sort((a, b) => a.position - b.position).map((row) => row.row_id);
      let metadataIndex = (used.values[0] || []).findIndex((value) => {
        const header = String(value).trim();
        return header === ROW_ID_HEADER || header === LEGACY_ROW_ID_HEADER;
      });
      if (metadataIndex < 0) {
        if (tables.items.length) {
          const identityColumn = tables.items[0].columns.add(null, null, ROW_ID_HEADER);
          await context.sync();
          const body = identityColumn.getDataBodyRange();
          body.load("rowCount");
          const identityRange = identityColumn.getRange();
          identityRange.load("columnIndex");
          await context.sync();
          metadataIndex = identityRange.columnIndex;
          if (body.rowCount > 0) {
            body.values = Array.from({ length: body.rowCount }, (_, index) => [expectedIds[index] || null]);
          }
          identityRange.getEntireColumn().format.columnHidden = true;
        } else {
          metadataIndex = used.columnCount;
          sheet.getRangeByIndexes(0, metadataIndex, 1, 1).values = [[ROW_ID_HEADER]];
          const dataRows = Math.max(used.rowCount - 1, expectedIds.length);
          if (dataRows > 0) {
            sheet.getRangeByIndexes(1, metadataIndex, dataRows, 1).values =
              Array.from({ length: dataRows }, (_, index) => [expectedIds[index] || null]);
          }
        }
        addLog(`Attached stable row identity to ${semantic.name} (${expectedIds.length} row(s)).`);
      } else {
        const existingIds = used.values.slice(1).map((row) => row[metadataIndex]);
        const validIdCount = existingIds.filter((value) => /^ROW_[A-Z0-9]+$/.test(String(value))).length;
        if (String((used.values[0] || [])[metadataIndex]).trim() !== ROW_ID_HEADER) {
          sheet.getRangeByIndexes(0, metadataIndex, 1, 1).values = [[ROW_ID_HEADER]];
        }
        if (validIdCount === 0 || (used.rowCount - 1 === expectedIds.length
          && existingIds.some((value, index) => String(value || "") !== expectedIds[index]))) {
          sheet.getRangeByIndexes(1, metadataIndex, Math.max(used.rowCount - 1, expectedIds.length), 1).values =
            Array.from({ length: Math.max(used.rowCount - 1, expectedIds.length) }, (_, index) => [expectedIds[index] || null]);
          addLog(`Repaired stable row identity on ${semantic.name}.`);
        }
      }
      sheet.getRangeByIndexes(0, metadataIndex, 1, 1).getEntireColumn().format.columnHidden = true;
      await context.sync();
    }
  }), [addLog]);

  const writeSnapshot = useCallback(async (snapshot) => {
    applyingRemoteRef.current = true;
    try {
      await window.Excel.run(async (context) => {
        const worksheets = context.workbook.worksheets;
        worksheets.load("items/id,name,position");
        await context.sync();
        const semanticSheets = [...(snapshot?.semantic?.sheets || [])].sort((a, b) => a.position - b.position);
        const claimed = new Set();
        for (const [sheetIndex, semantic] of semanticSheets.entries()) {
          let sheet = worksheets.items.find((item) => !claimed.has(item.id) && item.name === semantic.name)
            || worksheets.items.find((item) => !claimed.has(item.id) && item.position === semantic.position);
          if (!sheet) {
            sheet = worksheets.add(semantic.name);
            sheet.load("id,name,position");
            await context.sync();
          }
          claimed.add(sheet.id);
          if (sheet.name !== semantic.name) sheet.name = semantic.name;
          sheet.position = sheetIndex;
          const used = sheet.getUsedRangeOrNullObject(true);
          used.load(["isNullObject", "rowIndex", "columnIndex", "rowCount", "columnCount"]);
          const tables = sheet.tables;
          tables.load("items");
          await context.sync();

          const dataColumns = [...semantic.columns].sort((a, b) => a.position - b.position);
          const rows = [...semantic.rows].sort((a, b) => a.position - b.position);
          const output = [[...dataColumns.map((column) => column.name), ROW_ID_HEADER]];
          const formulaOutput = [[...dataColumns.map((column) => column.name), ROW_ID_HEADER]];
          rows.forEach((row) => {
            output.push([...dataColumns.map((column) => row.values[column.column_id] ?? null), row.row_id]);
            formulaOutput.push([
              ...dataColumns.map((column) => row.formulas?.[column.column_id] || (row.values[column.column_id] ?? "")),
              row.row_id,
            ]);
          });
          if (output.length === 1 && tables.items.length) {
            output.push(Array.from({ length: output[0].length }, () => null));
            formulaOutput.push(Array.from({ length: output[0].length }, () => null));
          }

          let startRow = 0;
          let startColumn = 0;
          let oldRowCount = used.isNullObject ? 0 : used.rowCount;
          let oldColumnCount = used.isNullObject ? 0 : used.columnCount;
          if (tables.items.length) {
            const tableRange = tables.items[0].getRange();
            tableRange.load(["rowIndex", "columnIndex", "rowCount", "columnCount"]);
            await context.sync();
            startRow = tableRange.rowIndex;
            startColumn = tableRange.columnIndex;
            oldRowCount = tableRange.rowCount;
            oldColumnCount = tableRange.columnCount;
            let target = sheet.getRangeByIndexes(startRow, startColumn, output.length, output[0].length);
            if (oldRowCount !== output.length || oldColumnCount !== output[0].length) {
              tables.items[0].resize(target);
              await context.sync();
              target = sheet.getRangeByIndexes(startRow, startColumn, output.length, output[0].length);
            }
            target.formulas = formulaOutput;
            if (oldRowCount > output.length) {
              sheet.getRangeByIndexes(startRow + output.length, startColumn, oldRowCount - output.length, oldColumnCount)
                .clear(window.Excel.ClearApplyTo.contents);
            }
            if (oldColumnCount > output[0].length) {
              sheet.getRangeByIndexes(startRow, startColumn + output[0].length, Math.min(oldRowCount, output.length), oldColumnCount - output[0].length)
                .clear(window.Excel.ClearApplyTo.contents);
            }
          } else {
            if (!used.isNullObject) used.clear(window.Excel.ClearApplyTo.contents);
            sheet.getRangeByIndexes(0, 0, output.length, output[0].length).formulas = formulaOutput;
          }
          if (dataColumns.length) {
            const header = sheet.getRangeByIndexes(startRow, startColumn, 1, dataColumns.length);
            header.format.font.bold = true;
            header.format.fill.color = "#1F6FEB";
            header.format.font.color = "#FFFFFF";
          }
          sheet.getRangeByIndexes(startRow, startColumn + dataColumns.length, 1, 1)
            .getEntireColumn().format.columnHidden = true;
        }
        worksheets.items.filter((sheet) => !claimed.has(sheet.id)).forEach((sheet) => sheet.delete());
        await context.sync();
      });
    } finally {
      setTimeout(() => { applyingRemoteRef.current = false; }, 350);
    }
  }, []);

  const reviewChanges = useCallback(async () => {
    if (!baselineRef.current) throw new Error("Connect to a dataset first.");
    const workbook = await readWorkbook();
    const diff = buildSemanticWorkbookDiff(baselineRef.current, workbook);
    setStaged(diff); setDirty(diff.changeCount > 0); setConflicts([]);
    setSyncStatus(diff.changeCount ? "syncing" : "connected");
    setStatusMsg(diff.changeCount ? `${diff.changeCount} staged change(s)` : "Working tree clean");
    addLog(diff.changeCount ? `Reviewed ${diff.changeCount} staged change(s).` : "Working tree matches the checked-out version.");
    if (diff.changeCount && tableId) {
      saveDraft(tableId, {
        base_head: baselineRef.current.head_commit_id, base_version: baselineRef.current.version,
        semantic_changes: diff.semantic_changes, commit_message: commitMessage,
      }).catch(() => {});
    } else if (tableId) {
      clearDraft(tableId).catch(() => {});
    }
    return diff;
  }, [addLog, clearDraft, commitMessage, readWorkbook, saveDraft, tableId]);

  useEffect(() => {
    reviewChangesRef.current = reviewChanges;
  }, [reviewChanges]);

  const pollFormulaExplanationRun = useCallback((runId, cell) => {
    let cancelled = false;
    const tick = async () => {
      try {
        const progress = await getAgentRunProgress(runId);
        if (cancelled) return;
        setFormulaExplainSteps(progress.steps || []);
        if (progress.run.status === "RUNNING") {
          setTimeout(tick, 900);
          return;
        }
        setFormulaExplainBusy(false);
        if (progress.run.status === "COMPLETED") {
          const explanation = await getFormulaExplanation(tableId, {
            branch_id: cell.branchId, sheet_name: cell.sheetName, cell_address: cell.cellAddress, formula: cell.formula,
          });
          if (!cancelled) setFormulaExplainResult(explanation);
        } else {
          addLog("Formula explanation failed — the AI provider may be unavailable right now.", true);
        }
      } catch (err) {
        if (!cancelled) { setFormulaExplainBusy(false); addLog(err.message, true); }
      }
    };
    tick();
    return () => { cancelled = true; };
  }, [addLog, tableId]);

  const explainSelectedFormula = useCallback(async () => {
    const branchId = workbookIdentityRef.current?.branch_id;
    if (!branchId) { addLog("This workbook has no signed branch identity.", true); return; }
    setFormulaExplainResult(null); setFormulaExplainSteps([]); setFormulaExplainRunId(null); setFormulaExplainBusy(true);
    try {
      const cell = await window.Excel.run(async (context) => {
        const range = context.workbook.getSelectedRange();
        range.load(["formulas", "address"]);
        const sheet = range.worksheet;
        sheet.load("name");
        await context.sync();
        const rawAddress = range.address || "";
        const bang = rawAddress.lastIndexOf("!");
        const sheetName = bang >= 0 ? rawAddress.slice(0, bang).replace(/^'|'$/g, "") : sheet.name;
        const cellAddress = bang >= 0 ? rawAddress.slice(bang + 1) : rawAddress;
        return { formula: range.formulas?.[0]?.[0], sheetName, cellAddress: cellAddress.split(":")[0] };
      });
      if (!cell.formula || typeof cell.formula !== "string" || !cell.formula.startsWith("=")) {
        setFormulaExplainBusy(false);
        addLog("Select a cell with a formula first.", true);
        return;
      }
      const fullCell = { ...cell, branchId };
      const started = await startFormulaExplanation(tableId, {
        branch_id: branchId, sheet_name: cell.sheetName, cell_address: cell.cellAddress, formula: cell.formula,
      });
      if (started.status === "COMPLETED") {
        setFormulaExplainBusy(false);
        setFormulaExplainResult(started);
      } else {
        setFormulaExplainRunId(started.agent_run_id);
        pollFormulaExplanationRun(started.agent_run_id, fullCell);
      }
    } catch (err) {
      setFormulaExplainBusy(false);
      addLog(err.message, true);
    }
  }, [addLog, pollFormulaExplanationRun, tableId]);

  const refreshAfterCommit = useCallback(async () => {
    const latest = await getWorkbookSnapshot(tableId);
    await writeSnapshot(latest);
    baselineRef.current = latest;
    await saveBaseVersion(latest.version, tableId, latest.head_commit_id);
    setBaseVersion(latest.version); setDirty(false); setStaged(null); setConflicts([]);
    return latest;
  }, [saveBaseVersion, tableId, writeSnapshot]);

  const commitChanges = useCallback(async () => {
    if (!commitMessage.trim()) { addLog("Enter a commit message first.", true); return; }
    setBusy(true); setSyncStatus("syncing"); setStatusMsg("Creating audited commit...");
    let commitPayload = null;
    try {
      const diff = await reviewChanges();
      if (!diff.changeCount) return;
      commitPayload = {
        table_id: tableId,
        ...(workbookIdentityRef.current || {}),
        base_version: baselineRef.current.version,
        expected_head_commit_id: baselineRef.current.head_commit_id,
        semantic_changes: diff.semantic_changes,
        source: "excel_commit",
        commit_message: commitMessage.trim(),
      };
      const result = await commitWorkbook(commitPayload);
      addLog(`Committed ${result.commit_id} as version ${result.version}; ${result.change_count} semantic operation(s).`);
      setCommitMessage("");
      setLastCommitId(result.commit_id);
      clearDraft(tableId).catch(() => {});
      clearPendingCommit(tableId).catch(() => {});
      setPendingCommit(null); setRecoveredDraft(null);
      try {
        await refreshAfterCommit();
        setSyncStatus("synced");
        setStatusMsg(`Committed version ${result.version}`);
      } catch (refreshError) {
        setSyncStatus("error");
        setStatusMsg(`Version ${result.version} committed; Excel refresh needs Pull`);
        addLog(
          `Server commit succeeded, but Excel refresh failed: ${refreshError.message}. Click Pull latest to repair the workbook.`,
          true
        );
      }
    } catch (err) {
      setSyncStatus("error"); setStatusMsg(err.message);
      if (err.detail?.code === "DEVICE_BLOCKED") {
        await handleDeviceBlocked();
      } else if (err.detail?.code === "WORKING_COPY_CLOSED") {
        setWorkspaceClosed(true);
        setStatusMsg("Workspace merged");
        addLog("This branch was merged. Commit is disabled; create a new workspace from Git Walk.", true);
      } else if (err.status === 409) {
        const currentHead = err.detail?.current_head_commit_id || err.detail?.current_head || "a newer commit";
        setConflicts([`Branch HEAD advanced to ${currentHead}. Pull latest before committing.`]);
        addLog("Commit rejected because the server advanced. Pull latest to rebase your local work.", true);
      } else if ((!err.status || err.status >= 500) && commitPayload) {
        // No HTTP status at all => the request never reached the server
        // (network drop mid-flight). A 5xx means it reached the server but
        // failed transiently there. Either way the client can't tell if the
        // commit landed, so save the exact payload for retry instead of
        // losing it — a timeout/server hiccup shouldn't be worse than a
        // dropped connection. Conflict (409) and permission errors above
        // are deliberately excluded — retrying those blindly would be wrong.
        await savePendingCommit(tableId, commitPayload);
        setPendingCommit(loadPendingCommit(tableId));
        addLog("Network error during commit — saved as a pending commit. It will be offered for retry once you're back online.", true);
      } else addLog(`Commit failed: ${err.message}`, true);
    } finally { setBusy(false); }
  }, [addLog, clearDraft, clearPendingCommit, commitMessage, handleDeviceBlocked, loadPendingCommit, refreshAfterCommit, reviewChanges, savePendingCommit, tableId]);

  const retryPendingCommit = useCallback(async () => {
    if (!pendingCommit) return;
    setPendingCommitBusy(true);
    try {
      const { saved_at, ...payload } = pendingCommit;
      const result = await commitWorkbook(payload);
      addLog(`Pending commit retried successfully: ${result.commit_id} (version ${result.version}).`);
      setLastCommitId(result.commit_id);
      await clearPendingCommit(tableId);
      setPendingCommit(null);
      await refreshAfterCommit();
    } catch (err) {
      addLog(`Retry of pending commit failed: ${err.message}`, true);
    } finally { setPendingCommitBusy(false); }
  }, [addLog, clearPendingCommit, pendingCommit, refreshAfterCommit, tableId]);

  useEffect(() => {
    retryPendingCommitRef.current = retryPendingCommit;
  }, [retryPendingCommit]);

  const discardPendingCommit = useCallback(async () => {
    await clearPendingCommit(tableId);
    setPendingCommit(null);
  }, [clearPendingCommit, tableId]);

  const syncMain = useCallback(async () => {
    const branchId = workbookIdentityRef.current?.branch_id;
    if (!branchId) { addLog("This workbook has no signed branch identity.", true); return; }
    setBusy(true); setSyncStatus("syncing"); setStatusMsg("Pulling protected main...");
    try {
      const local = await reviewChanges();
      if (local.changeCount) {
        setStatusMsg("Commit local changes before pulling main");
        addLog("Main pull paused because the working tree has local changes. Commit them first.", true);
        return;
      }
      const result = await syncBranchWithMain(branchId);
      const latest = await getWorkbookSnapshot(tableId);
      await writeSnapshot(latest);
      baselineRef.current = latest;
      await saveBaseVersion(latest.version, tableId, latest.head_commit_id);
      const status = await getBranchDivergence(branchId);
      setDivergence(status); setBranchName((current) => status.source_branch_name || current);
      setBaseVersion(latest.version); setStaged(null); setDirty(false);
      setSyncStatus("synced"); setStatusMsg(result.status === "UP_TO_DATE" ? "Already current with main" : "Main synced");
      addLog(result.status === "UP_TO_DATE" ? "Branch is already current with main." : `Created sync commit ${result.commit_id}.`);
    } catch (err) {
      const mergeConflicts = err.detail?.conflicts || [];
      if (mergeConflicts.length) {
        setConflicts(mergeConflicts.map((item) => `${item.conflict_type}: ${item.row_id || item.column_id || item.sheet_id || "workbook"}`));
        setStatusMsg(`${mergeConflicts.length} main-sync conflict(s)`);
      } else setStatusMsg(err.message);
      setSyncStatus("error"); addLog(`Pull from main failed: ${err.message}`, true);
    } finally { setBusy(false); }
  }, [addLog, reviewChanges, saveBaseVersion, tableId, writeSnapshot]);

  const pullLatest = useCallback(async (discardLocal = false) => {
    setBusy(true); setSyncStatus("syncing"); setStatusMsg("Pulling server changes...");
    try {
      const diff = discardLocal ? null : await reviewChanges();
      const remote = await getWorkbookSnapshot(tableId);
      if (!discardLocal && diff?.changeCount) {
        const found = semanticRebaseConflicts(baselineRef.current, remote, diff);
        if (found.length) {
          setConflicts(found); setSyncStatus("error");
          setStatusMsg(`${found.length} merge conflict(s)`);
          addLog(`Pull paused with ${found.length} conflict(s). Local cells were not overwritten.`, true);
          return;
        }
        const rebased = applySemanticChanges(remote, diff.semantic_changes);
        await writeSnapshot(rebased);
        baselineRef.current = remote;
        await saveBaseVersion(remote.version, tableId, remote.head_commit_id);
        setBaseVersion(remote.version); setDirty(true); setStaged(null); setConflicts([]);
        setSyncStatus("syncing"); setStatusMsg("Pulled and reapplied local changes");
        addLog(`Pulled version ${remote.version} and rebased ${diff.changeCount} local change(s). Review and commit next.`);
      } else {
        await writeSnapshot(remote);
        baselineRef.current = remote;
        await saveBaseVersion(remote.version, tableId, remote.head_commit_id);
        setBaseVersion(remote.version); setDirty(false); setStaged(null); setConflicts([]);
        setSyncStatus("synced"); setStatusMsg(`Pulled version ${remote.version}`);
        addLog(`Pulled server version ${remote.version}. Working tree is clean.`);
      }
    } catch (err) {
      setSyncStatus("error"); setStatusMsg(err.message); addLog(`Pull failed: ${err.message}`, true);
    } finally { setBusy(false); }
  }, [addLog, reviewChanges, saveBaseVersion, tableId, writeSnapshot]);

  const disconnect = useCallback(async () => {
    clearInterval(presenceTimerRef.current);
    presenceTimerRef.current = null;
    workStatusRef.current = "ONLINE";
    setWorkStatus("ONLINE");
    if (tableId) leaveDatasetPresence(tableId, clientIdRef.current).catch(() => {});
    try {
      await window.Excel.run(async (context) => {
        const worksheets = context.workbook.worksheets;
        worksheets.load("items/id");
        await context.sync();
        for (const binding of handlerRef.current) {
          if (binding.kind === "changed") {
            const sheet = worksheets.items.find((item) => item.id === binding.sheetId);
            if (sheet) sheet.onChanged.remove(binding.handler);
          }
          if (binding.kind === "added") worksheets.onAdded.remove(binding.handler);
          if (binding.kind === "deleted") worksheets.onDeleted.remove(binding.handler);
        }
        await context.sync();
      });
    } catch (err) { addLog(`Could not unbind: ${err.message}`, true); }
    handlerRef.current = []; baselineRef.current = null;
    setConnected(false); setSyncStatus("idle"); setStatusMsg(null); setStaged(null); setDirty(false);
    setDivergence(null); setWorkspaceClosed(false); setPullSource("branch");
  }, [addLog, tableId]);

  const connect = useCallback(async (requestedTableId = tableId) => {
    const normalized = String(requestedTableId || "").trim().toUpperCase();
    if (!normalized) return;
    deviceBlockedRef.current = false; setDeviceBlocked(false);
    setBusy(true); setTableId(normalized); setSyncStatus("syncing"); setStatusMsg("Checking out workbook...");
    try {
      const latest = await getWorkbookSnapshot(normalized);
      const savedTableId = Office.context.document.settings.get("tableId");
      const keyedVersion = Office.context.document.settings.get(`baseVersion:${normalized}`);
      const legacyVersion = savedTableId === normalized
        ? Office.context.document.settings.get("baseVersion") : null;
      const branchId = workbookIdentityRef.current?.branch_id;
      const savedHead = Office.context.document.settings.get(`baseHead:${normalized}`)
        || workbookIdentityRef.current?.base_commit_id;
      const hasSavedVersion = keyedVersion != null || legacyVersion != null;
      const savedVersion = Number(keyedVersion ?? legacyVersion);
      let baseline = latest;
      if (branchId && savedHead && savedHead !== latest.head_commit_id) {
        try {
          const state = await getBranchState(branchId, savedHead);
          baseline = {
            ...latest, semantic: state, head_commit_id: savedHead,
            version: hasSavedVersion && Number.isInteger(savedVersion) ? savedVersion : latest.version,
          };
        } catch {
          addLog(`Saved checkout ${savedHead} was unavailable; using current branch HEAD.`, true);
        }
      }
      await ensureRowIdentity(baseline);
      const workbook = await readWorkbook();
      baselineRef.current = baseline;
      setBaseVersion(baseline.version);

      const draft = loadDraft(normalized);
      if (draft?.semantic_changes?.length) {
        setRecoveredDraft(draft);
        addLog(`Recovered ${draft.semantic_changes.length} unsaved change(s) from a previous session.`);
      } else {
        setRecoveredDraft(null);
      }
      const pending = loadPendingCommit(normalized);
      if (pending) {
        setPendingCommit(pending);
        addLog("A previously failed commit is saved locally and can be retried.");
      } else {
        setPendingCommit(null);
      }

      await window.Excel.run(async (context) => {
        const worksheets = context.workbook.worksheets;
        worksheets.load("items/id,name");
        await context.sync();
        const markDirty = () => {
          if (applyingRemoteRef.current || deviceBlockedRef.current) return;
          setDirty(true); setStaged(null); setConflicts([]);
          setSyncStatus("syncing"); setStatusMsg("Local changes pending review");
          heartbeatPresence(normalized, clientIdRef.current, "excel", "editing", workStatusRef.current).catch((err) => {
            if (err.detail?.code === "DEVICE_BLOCKED") handleDeviceBlocked(normalized);
          });
        };
        const bindings = worksheets.items.map((sheet) => {
          const handler = () => markDirty();
          sheet.onChanged.add(handler);
          return { kind: "changed", sheetId: sheet.id, handler };
        });
        const addedHandler = () => markDirty();
        const deletedHandler = () => markDirty();
        worksheets.onAdded.add(addedHandler);
        worksheets.onDeleted.add(deletedHandler);
        bindings.push({ kind: "added", handler: addedHandler });
        bindings.push({ kind: "deleted", handler: deletedHandler });
        handlerRef.current = bindings;
        await context.sync();
      });
      Office.context.document.settings.set("tableId", normalized);
      Office.context.document.settings.set(`baseHead:${normalized}`, baseline.head_commit_id);
      Office.context.document.settings.saveAsync(() => {});
      setConnected(true); setSyncStatus("connected");
      if (workbookIdentityRef.current?.branch_id) {
        try {
          const branchStatus = await getBranchDivergence(workbookIdentityRef.current.branch_id);
          setDivergence(branchStatus);
          setBranchName((current) => branchStatus.source_branch_name || current);
          setWorkspaceClosed(branchStatus.source_status === "MERGED");
        } catch { setDivergence(null); }
      }
      const heartbeat = () => heartbeatPresence(
        normalized, clientIdRef.current, "excel", "viewing", workStatusRef.current
      ).catch((err) => {
        if (err.detail?.code === "DEVICE_BLOCKED") handleDeviceBlocked(normalized);
      });
      heartbeat();
      clearInterval(presenceTimerRef.current);
      presenceTimerRef.current = setInterval(heartbeat, 60000);
      setStatusMsg(latest.version > baseline.version ? `Server ahead: v${latest.version}` : `Checked out v${baseline.version}`);
      addLog(`Checked out version ${baseline.version}. Edits stay local until Commit.`);
      if (latest.version > baseline.version) addLog(`Server version ${latest.version} is available. Pull before committing.`);
    } catch (err) {
      setSyncStatus("error"); setStatusMsg(err.message); addLog(`Connection failed: ${err.message}`, true);
    } finally { setBusy(false); }
  }, [addLog, ensureRowIdentity, handleDeviceBlocked, loadDraft, loadPendingCommit, readWorkbook, tableId]);

  const getEmbeddedIdentity = useCallback(async () => {
    try {
      return await window.Excel.run(async (context) => {
        const definitions = { table_id: TABLE_ID_DEFINED_NAME, ...WORKBOOK_METADATA_NAMES };
        const items = Object.fromEntries(
          Object.entries(definitions).map(([key, name]) => {
            const item = context.workbook.names.getItemOrNullObject(name);
            item.load("formula");
            return [key, item];
          })
        );
        await context.sync();
        const identity = {};
        Object.entries(items).forEach(([key, item]) => {
          if (!item.isNullObject) identity[key] = parseDefinedValue(item.formula);
        });
        identity.table_id = parseTableIdFormula(identity.table_id);
        return identity.table_id ? identity : null;
      });
    } catch { return null; }
  }, []);

  useEffect(() => {
    if (autoAuthAttemptedRef.current) return;
    autoAuthAttemptedRef.current = true;
    (async () => {
      try {
        const embedded = await getEmbeddedIdentity();
        if (!embedded?.working_copy_id) {
          setAuthBootstrapping(false);
          return;
        }
        setEmbeddedInfo(embedded);
        setBranchName(embedded.branch_name || "");
        workbookIdentityRef.current = Object.fromEntries(
          Object.entries(embedded).filter(([key]) => key !== "table_id" && key !== "branch_name")
        );

        // Path Verification check:
        if (embedded.local_file_path) {
          const docUrl = (typeof Office !== "undefined" && Office?.context?.document?.url)
            ? Office.context.document.url
            : "";
          const expected = normalizeFilePath(embedded.local_file_path);
          const current = normalizeFilePath(docUrl);
          if (!current || expected !== current) {
            setPathBlocked(true);
            setPathDetails({
              expected: embedded.local_file_path,
              current: docUrl || "Unknown location (file outside authorized directory)",
            });
            addLog(`Security Alert: Workbook path mismatch. Expected: ${embedded.local_file_path}, Current: ${docUrl}`, true);
            await purgeWorkbookData("[LOCKED] Unauthorized file location. Verification blocked.");
            setAuthBootstrapping(false);
            return;
          }
          addLog(`Path verification passed for authorized location.`);
        }

        // Ensure data rows remain locked and empty until OTP verification completes
        if (!workbookVerified) {
          await purgeWorkbookData("[LOCKED] Identity & OTP verification required to load branch data.");
        }
      } catch (err) {
        addLog(`Workbook initialization check failed: ${err.message}`, true);
      } finally {
        setAuthBootstrapping(false);
      }
    })();
  }, [addLog, getEmbeddedIdentity, workbookVerified]);

  const handleVerifiedAndLoaded = useCallback(async (result) => {
    setBusy(true);
    try {
      setAuth({ token: result.token, user: result.user });
      setWorkbookVerified(true);
      setTableId(result.table_id);
      addLog(`Verified role '${result.role}' for ${result.user.email}.`);
      if (result.snapshot) {
        addLog("Loading branch dataset into workbook...");
        await writeSnapshot(result.snapshot);
        baselineRef.current = result.snapshot;
        await saveBaseVersion(result.snapshot.version, result.table_id, result.snapshot.head_commit_id);
        setBaseVersion(result.snapshot.version);
      }
      await connect(result.table_id);
      addLog("Branch data loaded and synchronized. Ready to edit.");
    } catch (err) {
      addLog(`Failed loading branch data: ${err.message}`, true);
    } finally {
      setBusy(false);
    }
  }, [addLog, connect, saveBaseVersion, writeSnapshot]);

  useEffect(() => {
    if (!auth || autoConnectedRef.current) return;
    if (embeddedInfo?.working_copy_id && !workbookVerified) return;
    autoConnectedRef.current = true;
    (async () => {
      const embedded = await getEmbeddedIdentity();
      const saved = Office.context.document.settings.get("tableId");
      setBranchName(embedded?.branch_name || "");
      workbookIdentityRef.current = embedded
        ? Object.fromEntries(Object.entries(embedded).filter(([key]) => key !== "table_id" && key !== "branch_name"))
        : null;
      if (embedded?.table_id || saved) await connect(embedded?.table_id || saved);
      else addLog("No embedded Table ID found. Enter it once to connect.");
    })();
  }, [addLog, auth, connect, embeddedInfo, getEmbeddedIdentity, workbookVerified]);

  const updateWorkStatus = useCallback((status) => {
    setWorkStatus(status);
    workStatusRef.current = status;
    if (tableId) {
      heartbeatPresence(tableId, clientIdRef.current, "excel", "viewing", status).catch(() => {});
    }
  }, [tableId]);

  const signOut = useCallback(async () => {
    if (connected) await disconnect();
    await logout().catch(() => clearAuth());
    setAuth(null);
    setWorkbookVerified(false);
    setTableId("");
    setBranchName("");
    setLogs([]);
    autoConnectedRef.current = false;
    workbookIdentityRef.current = null;
    await purgeWorkbookData("[LOCKED] Signed out. Verification required.");
    if (embeddedInfo?.working_copy_id || embeddedInfo?.local_file_path) {
      sanitizeLocalWorkbook({
        working_copy_id: embeddedInfo?.working_copy_id,
        file_path: embeddedInfo?.local_file_path,
      }).catch(() => {});
    }
  }, [connected, disconnect, embeddedInfo]);

  useEffect(() => {
    const handleBeforeUnload = () => {
      if (embeddedInfo?.working_copy_id || embeddedInfo?.local_file_path) {
        const payload = JSON.stringify({
          working_copy_id: embeddedInfo.working_copy_id,
          file_path: embeddedInfo.local_file_path,
        });
        if (navigator.sendBeacon) {
          const blob = new Blob([payload], { type: "application/json" });
          navigator.sendBeacon("/api/v1/workbooks/sanitize-local", blob);
        }
      }
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [embeddedInfo]);

  if (pathBlocked) {
    return (
      <PathBlockedPanel
        expectedPath={pathDetails.expected}
        currentPath={pathDetails.current}
        onWipeData={() => purgeWorkbookData("[LOCKED] Unauthorized file location. Verification blocked.")}
      />
    );
  }

  if (authBootstrapping) return (
    <div style={containerStyle}>
      <div style={{ ...cardStyle, marginTop: 30 }}>
        <div style={labelStyle}>SIGNED BRANCH</div>
        <h2 style={{ margin: "8px 0" }}>Verifying location & branch</h2>
        <p style={{ color: "#8fa4af", fontSize: 11, lineHeight: 1.6 }}>
          Checking file path authorization and loading workspace...
        </p>
      </div>
    </div>
  );

  if (embeddedInfo?.working_copy_id && !workbookVerified) {
    return (
      <WorkbookVerificationPanel
        embeddedInfo={embeddedInfo}
        onVerifiedAndLoaded={handleVerifiedAndLoaded}
      />
    );
  }

  if (!auth?.token) return <AuthPanel onAuthenticated={setAuth} />;
  const summary = staged || {
    changeCount: dirty ? "?" : 0,
    counts: { cells: 0, rows: 0, columns: 0, formulas: 0, sheets: 0 },
    preview: [],
  };

  return (
    <div style={containerStyle}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div><div style={{ color: "#58a6ff", fontSize: 10, fontWeight: 800, letterSpacing: ".13em" }}>GIT WALK / SOURCE CONTROL</div><div style={{ fontSize: 20, fontWeight: 800 }}>Repository workspace</div></div>
        <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
          <button
            type="button"
            onClick={async () => {
              if (window.confirm("Lock sheets and revert file to 9KB protected state?")) {
                await purgeWorkbookData("[LOCKED] Locked & protected. Verification required.");
                if (embeddedInfo?.working_copy_id || embeddedInfo?.local_file_path) {
                  sanitizeLocalWorkbook({
                    working_copy_id: embeddedInfo?.working_copy_id,
                    file_path: embeddedInfo?.local_file_path,
                  }).catch(() => {});
                }
                setWorkbookVerified(false);
                setConnected(false);
              }
            }}
            style={{
              border: "1px solid #f85149",
              color: "#ff8b82",
              background: "rgba(248,81,73,0.12)",
              borderRadius: 4,
              padding: "4px 8px",
              fontSize: 10,
              fontWeight: 700,
              cursor: "pointer",
            }}
            title="Save to protected 9KB state and wipe sheets before closing Excel"
          >
            🔒 Lock & Exit (9KB)
          </button>
          <button onClick={signOut} style={{ border: 0, color: "#92a6af", background: "transparent", cursor: "pointer", fontSize: 11 }}>Sign out</button>
        </div>
      </div>
      <div style={{ ...cardStyle, padding: 11, display: "flex", justifyContent: "space-between" }}>
        <div><div style={{ fontSize: 11 }}>{auth.user?.email}</div><div style={{ color: "#70858f", fontSize: 9, marginTop: 3 }}>{auth.user?.user_id}</div></div>
        <div style={{ color: "#75ead0", fontSize: 10, fontWeight: 800 }}>BASE v{baseVersion}</div>
      </div>
      <StatusBanner status={syncStatus} message={statusMsg} />
      <div style={cardStyle}><div style={labelStyle}>Repository data ID</div><input style={inputStyle} value={tableId} onChange={(e) => setTableId(e.target.value.toUpperCase())} disabled={connected} placeholder="QUEUE_BOARD_A1B2C3D4" /></div>

      {!connected ? <button style={buttonStyle} disabled={busy || !tableId} onClick={() => connect()}>{busy ? "Connecting..." : "Open repository workspace"}</button> : <>
        <div style={{ ...cardStyle, padding: 10 }}>
          <div style={{ ...labelStyle, marginBottom: 7 }}>Your status</div>
          <div style={{ display: "flex", gap: 6 }}>
            <button
              onClick={() => updateWorkStatus("ONLINE")}
              style={{
                flex: 1, padding: "7px 6px", borderRadius: 5, fontSize: 10, fontWeight: 800, cursor: "pointer",
                border: workStatus === "ONLINE" ? "1px solid #3fb950" : "1px solid #30363d",
                background: workStatus === "ONLINE" ? "rgba(63,185,80,0.16)" : "transparent",
                color: workStatus === "ONLINE" ? "#7ee787" : "#8b949e",
              }}
            >🟢 Online</button>
            <button
              onClick={() => updateWorkStatus("NEED_HELP")}
              style={{
                flex: 1, padding: "7px 6px", borderRadius: 5, fontSize: 10, fontWeight: 800, cursor: "pointer",
                border: workStatus === "NEED_HELP" ? "1px solid #d29922" : "1px solid #30363d",
                background: workStatus === "NEED_HELP" ? "rgba(210,153,34,0.16)" : "transparent",
                color: workStatus === "NEED_HELP" ? "#f0b849" : "#8b949e",
              }}
              title="Notifies the repository owner"
            >🆘 Need help</button>
          </div>
          {workStatus === "NEED_HELP" ? <div style={{ marginTop: 7, color: "#f0b849", fontSize: 9 }}>The repository owner has been notified. Switch back to Online once you're unblocked.</div> : null}
        </div>
        {deviceBlocked ? <div style={{ ...cardStyle, borderColor: "#f85149", background: "#2a191c" }}><div style={{ ...labelStyle, color: "#ff8b82" }}>🔒 Device blocked</div><div style={{ color: "#ffaaa3", fontSize: 11, lineHeight: 1.55 }}>The repository owner blocked this device. Data has been wiped from this workbook; any uncommitted edits were saved locally and will be offered for recovery once access is restored. Contact the owner to unblock this device.</div></div> : null}
        {workspaceClosed ? <div style={{ ...cardStyle, borderColor: "#b9862d", background: "#2a2114" }}><div style={{ ...labelStyle, color: "#f4ba62" }}>Workspace merged</div><div style={{ color: "#e9d7b5", fontSize: 11, lineHeight: 1.55 }}>This signed working copy is closed. Download latest main or create a new workspace in Git Walk.</div></div> : null}
        {divergence ? <div style={{ ...cardStyle, padding: 10, display: "flex", justifyContent: "space-between", color: "#9bafb8", fontSize: 10 }}><span><b style={{ color: "#75ead0" }}>{divergence.ahead}</b> ahead</span><span><b style={{ color: "#f4ba62" }}>{divergence.behind}</b> behind main</span></div> : null}
        <button style={{ ...buttonStyle, color: "#d8e5e9", background: "#172632", boxShadow: "none" }} disabled={busy || workspaceClosed || deviceBlocked} onClick={reviewChanges}>Review local changes</button>
        <div style={{ ...cardStyle, padding: 11 }}>
          <div style={labelStyle}>Pull data from</div>
          <select
            style={{ ...inputStyle, appearance: "auto", cursor: "pointer" }}
            value={pullSource}
            disabled={busy || workspaceClosed || deviceBlocked}
            onChange={(event) => setPullSource(event.target.value)}
          >
            <option value="main">Protected main{divergence?.target_branch_name ? ` / ${divergence.target_branch_name}` : ""}</option>
            <option value="branch">My branch / {branchName || divergence?.source_branch_name || workbookIdentityRef.current?.branch_id || "signed workspace"}</option>
          </select>
          <div style={{ color: "#718792", fontSize: 9, lineHeight: 1.5, marginTop: 7 }}>
            {pullSource === "main"
              ? "Reconcile protected main into your branch, then refresh this workbook."
              : "Refresh this workbook from your personal branch without touching main."}
          </div>
          <button
            style={{ ...buttonStyle, marginTop: 10, color: "#071716", background: "linear-gradient(135deg,#66c4ff,#49a8e8)" }}
            disabled={busy || workspaceClosed || deviceBlocked}
            onClick={() => (pullSource === "main" ? syncMain() : pullLatest(false))}
          >
            {pullSource === "main" ? "Pull from protected main" : "Pull from my branch"}
          </button>
        </div>
        <style>{"@keyframes gitwalk-spin { to { transform: rotate(360deg); } }"}</style>
        <div style={{ ...cardStyle, padding: 11, borderColor: "#5a3d99" }}>
          <div style={{ ...labelStyle, color: "#c297ff" }}>Formula Explainer Agent</div>
          <div style={{ color: "#a999c2", fontSize: 10, lineHeight: 1.5, marginBottom: 8 }}>
            Select a cell with a formula in Excel, then ask the agent to trace its real dependencies and edit history and explain it in plain language.
          </div>
          <button
            style={{ ...buttonStyle, background: "#6639ba", borderColor: "#8250df", boxShadow: "0 4px 14px rgba(130,80,223,.25)" }}
            disabled={formulaExplainBusy || busy} onClick={explainSelectedFormula}
          >{formulaExplainBusy ? "Explaining..." : "Explain selected formula"}</button>
          {formulaExplainBusy ? (
            <div style={{ marginTop: 10 }}>
              {formulaExplainSteps.map((step) => (
                <div key={step.step_id} style={{ display: "flex", gap: 8, alignItems: "flex-start", marginBottom: 6 }}>
                  <span style={{ width: 18, height: 18, flexShrink: 0, borderRadius: "50%", background: "#3fb950", color: "#fff", fontSize: 10, display: "grid", placeItems: "center" }}>&#10003;</span>
                  <div><strong style={{ display: "block", fontSize: 10, color: "#edf8f7" }}>{step.step_type.replace(/_/g, " ")}</strong><span style={{ fontSize: 9, color: "#8399a5" }}>{step.description}</span></div>
                </div>
              ))}
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <span style={{ width: 18, height: 18, flexShrink: 0, borderRadius: "50%", border: "2px solid #8250df", borderTopColor: "transparent", animation: "gitwalk-spin 0.8s linear infinite" }} />
                <span style={{ fontSize: 9, color: "#c297ff" }}>Working on the next step...</span>
              </div>
            </div>
          ) : null}
          {!formulaExplainBusy && formulaExplainResult ? (
            <div style={{ marginTop: 10, padding: 10, borderRadius: 5, background: "#1e1730", border: "1px solid #5a3d99" }}>
              {formulaExplainResult.cache_hit ? <div style={{ fontSize: 8, color: "#8399a5", marginBottom: 5 }}>Served instantly from a prior explanation of this exact formula.</div> : null}
              <div style={{ fontSize: 11, color: "#edf8f7", lineHeight: 1.5 }}>{formulaExplainResult.summary}</div>
              {(formulaExplainResult.steps || []).length ? (
                <ol style={{ margin: "8px 0 0", paddingLeft: 16, color: "#c9b8e8", fontSize: 10 }}>
                  {formulaExplainResult.steps.map((step, index) => (<li key={index} style={{ marginBottom: 4 }}><b>{step.title}:</b> {step.explanation}</li>))}
                </ol>
              ) : null}
              {formulaExplainResult.risk_note ? (
                <div style={{ marginTop: 8, padding: 8, borderRadius: 4, background: "#2a2114", color: "#f4ba62", fontSize: 9.5 }}>&#9888; {formulaExplainResult.risk_note}</div>
              ) : null}
            </div>
          ) : null}
        </div>
        <CommitReviewPanel
          summary={summary}
          commitMessage={commitMessage}
          setCommitMessage={setCommitMessage}
          busy={busy}
          workspaceClosed={workspaceClosed || deviceBlocked}
          onCommit={commitChanges}
          lastCommitId={lastCommitId}
          recoveredDraft={recoveredDraft}
          onReviewRecoveredDraft={() => reviewChanges().catch((err) => addLog(err.message, true))}
          onDiscardDraft={() => { clearDraft(tableId).catch(() => {}); setRecoveredDraft(null); }}
          pendingCommit={pendingCommit}
          pendingCommitBusy={pendingCommitBusy}
          onRetryPendingCommit={retryPendingCommit}
          onDiscardPendingCommit={discardPendingCommit}
        />
        <button style={{ ...buttonStyle, color: "#ffaaa3", background: "#2a191c", boxShadow: "none" }} onClick={disconnect}>Disconnect</button>
      </>}

      {conflicts.length ? <div style={{ ...cardStyle, borderColor: "rgba(255,127,117,.45)" }}><div style={{ ...labelStyle, color: "#ff8b82" }}>Merge conflicts</div>{conflicts.slice(0, 12).map((item, index) => <div key={index} style={{ color: "#ffaaa3", fontSize: 10, padding: "4px 0" }}>{item}</div>)}<button style={{ ...buttonStyle, marginTop: 10, color: "#fff", background: "#b84540" }} onClick={() => { if (window.confirm("Discard every local uncommitted change and pull the server version?")) pullLatest(true); }}>Discard local changes and pull</button></div> : null}

      <div><div style={labelStyle}>Activity log</div><div style={{ ...cardStyle, maxHeight: 230, overflowY: "auto", padding: 11 }}>{logs.length ? logs.map((log, index) => <div key={`${log.time}-${index}`} style={{ color: log.isError ? "#ff8b82" : "#9bafb8", borderBottom: "1px solid #20303a", padding: "5px 0", font: "10px Consolas,monospace", lineHeight: 1.5 }}><span style={{ color: "#60747e" }}>[{log.time}]</span> {log.message}</div>) : <div style={{ color: "#60747e", fontSize: 11 }}>No activity yet.</div>}<div ref={logEndRef} /></div></div>
    </div>
  );
}
