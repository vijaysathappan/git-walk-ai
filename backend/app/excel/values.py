"""Canonical spreadsheet value handling shared by diff and commit paths."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from numbers import Real
from typing import Any


EXCEL_EPOCH = datetime(1899, 12, 30)
DATE_TEXT = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})(?:(?P<separator>[ T])"
    r"(?P<time>\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?)(?P<zone>Z|[+-]\d{2}:?\d{2})?)?$"
)


def _date_text(value: Any) -> tuple[datetime, re.Match[str]] | None:
    if not isinstance(value, str):
        return None
    match = DATE_TEXT.fullmatch(value.strip())
    if not match:
        return None
    candidate = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed, match


def _excel_datetime(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    serial = float(value)
    if not 1 <= serial <= 2_958_465:
        return None
    try:
        return EXCEL_EPOCH + timedelta(days=serial)
    except OverflowError:
        return None


def _format_like_reference(value: datetime, reference: str | None) -> str:
    parsed_reference = _date_text(reference)
    if parsed_reference:
        _, match = parsed_reference
        if not match.group("time"):
            return value.date().isoformat()
        separator = match.group("separator") or " "
        time_text = match.group("time") or ""
        if "." in time_text:
            digits = len(time_text.rsplit(".", 1)[1])
            rendered = value.isoformat(sep=separator, timespec="microseconds")
            return rendered[: -(6 - digits)] if digits < 6 else rendered
        timespec = "seconds" if time_text.count(":") == 2 else "minutes"
        return value.isoformat(sep=separator, timespec=timespec)
    return value.isoformat(sep=" ", timespec="seconds")


def normalize_excel_value(
    value: Any, *, reference: Any = None, data_type: str | None = None
) -> Any:
    """Convert Excel date serials to stable ISO text when the cell is date-like."""
    excel_date = _excel_datetime(value)
    reference_date = _date_text(reference)
    typed_as_date = bool(re.search(r"DATE|TIME", str(data_type or ""), re.IGNORECASE))
    if excel_date and (reference_date or typed_as_date):
        return _format_like_reference(
            excel_date, reference if isinstance(reference, str) else None
        )
    return value


def values_semantically_equal(left: Any, right: Any) -> bool:
    """Compare values while treating equivalent ISO dates and Excel serials equally."""
    if left is None or left == "":
        return right is None or right == ""
    if right is None or right == "":
        return False
    left_date = _date_text(left)
    right_date = _date_text(right)
    if left_date and right_date:
        return abs((left_date[0] - right_date[0]).total_seconds()) < 0.001
    if left_date:
        serial_date = _excel_datetime(right)
        if serial_date:
            return abs((left_date[0] - serial_date).total_seconds()) < 0.001
    if right_date:
        serial_date = _excel_datetime(left)
        if serial_date:
            return abs((right_date[0] - serial_date).total_seconds()) < 0.001
    return left == right or str(left) == str(right)
