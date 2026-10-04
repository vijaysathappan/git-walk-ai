"""Small contracts shared by EUC analyzers."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AnalysisWarning:
    code: str
    message: str
    severity: str = "WARNING"
    sheet_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class AnalysisContext:
    euc_id: str
    analysis_id: str
    file_path: Path
    file_type: str
    repository_id: str
    stable_sheets: dict[str, str]
    workbook: Any = None


@dataclass
class AnalyzerResult:
    analyzer_name: str
    status: str = "COMPLETED"
    metrics: dict[str, Any] = field(default_factory=dict)
    records: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    warnings: list[AnalysisWarning] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
