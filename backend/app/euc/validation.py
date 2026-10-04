"""Hostile-file-aware validation for supported EUC formats."""

import csv
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from ..config import settings


class EUCValidationError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass
class ValidationResult:
    valid: bool
    file_type: str
    mime_type: str
    encrypted: bool = False
    has_macros: bool = False
    warnings: list[dict[str, str]] = field(default_factory=list)


MIME_TYPES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    "csv": "text/csv",
}


def validate_euc_file(filename: str, payload: bytes) -> ValidationResult:
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix not in MIME_TYPES:
        raise EUCValidationError("UNSUPPORTED_FORMAT", "Stage 2.1 supports .xlsx, .xlsm, and .csv files.")
    if not payload:
        raise EUCValidationError("INVALID_FILE", "The uploaded file is empty.")
    if len(payload) > settings.euc_max_file_bytes:
        raise EUCValidationError("OVERSIZED_WORKBOOK", "The uploaded EUC exceeds the configured size limit.")
    if suffix == "csv":
        try:
            sample = payload[:65536].decode("utf-8-sig")
            list(csv.reader(io.StringIO(sample)))
        except (UnicodeDecodeError, csv.Error) as exc:
            raise EUCValidationError("INVALID_FILE", "CSV must be valid UTF-8 text.") from exc
        return ValidationResult(True, suffix, MIME_TYPES[suffix])

    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as package:
            entries = package.infolist()
            if len(entries) > settings.euc_max_zip_entries:
                raise EUCValidationError("ZIP_BOMB", "Workbook contains too many package entries.")
            total_size = 0
            names = set()
            for entry in entries:
                path = PurePosixPath(entry.filename.replace("\\", "/"))
                if path.is_absolute() or ".." in path.parts:
                    raise EUCValidationError("PATH_TRAVERSAL", "Workbook contains an unsafe package path.")
                total_size += entry.file_size
                if total_size > settings.euc_max_decompressed_bytes:
                    raise EUCValidationError("ZIP_BOMB", "Workbook decompressed size exceeds the safety limit.")
                if entry.filename.lower().endswith(".xml") and entry.file_size > settings.euc_max_xml_bytes:
                    raise EUCValidationError("OVERSIZED_XML", "Workbook contains an oversized XML part.")
                names.add(entry.filename)
            required = {"[Content_Types].xml", "xl/workbook.xml"}
            if not required.issubset(names):
                raise EUCValidationError("CORRUPT_OPENXML", "Required OpenXML workbook parts are missing.")
            encrypted = "EncryptedPackage" in names or "EncryptionInfo" in names
            if encrypted:
                raise EUCValidationError("PASSWORD_PROTECTED", "Password-protected workbooks cannot be inventoried.")
            has_macros = "xl/vbaProject.bin" in names
    except zipfile.BadZipFile as exc:
        raise EUCValidationError("CORRUPT_OPENXML", "The workbook is not a valid OpenXML ZIP package.") from exc
    if suffix == "xlsx" and has_macros:
        raise EUCValidationError("INVALID_FILE", "Macro content is not allowed inside an .xlsx container.")
    return ValidationResult(True, suffix, MIME_TYPES[suffix], False, has_macros)
