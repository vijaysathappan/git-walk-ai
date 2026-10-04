"""Small contracts shared by the Virtual Run macro pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


#: Names that mark a Sub as event-triggered rather than user-invoked.
#: Virtual Run never lists these as runnable -- they assume a live Excel
#: session context, not a one-shot server-side invocation.
AUTO_EXEC_NAMES = {
    "auto_open", "auto_close", "auto_exec",
    "workbook_open", "workbook_beforeclose", "workbook_beforesave",
    "worksheet_change", "worksheet_selectionchange", "worksheet_activate",
}


@dataclass
class BlockReason:
    line: int | None
    construct: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"line": self.line, "construct": self.construct, "reason": self.reason}


@dataclass
class ExtractedMacro:
    module_name: str
    proc_name: str
    source: str
    is_auto_exec: bool
    has_parameters: bool


@dataclass
class ExtractionOutcome:
    macros: list[ExtractedMacro] = field(default_factory=list)
    error_message: str | None = None
