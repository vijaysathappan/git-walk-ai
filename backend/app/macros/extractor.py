"""Real VBA source extraction for Virtual Run.

Supersedes Stage 2.1's blind regex byte-scan over the raw, still-compressed
``vbaProject.bin`` (``app/euc/analyzers.py`` ``OpenXMLAnalyzer``) with actual
MS-OVBA decompression via ``oletools.olevba``, giving genuine module/Sub
source text -- not a heuristic guess at identifier-shaped byte sequences.
That Stage 2.1 scan is left untouched; it still feeds migration-risk
scoring and is not replaced by this module.
"""

from __future__ import annotations

import io
import re
from typing import Any

from oletools.olevba import VBA_Parser

from .models import AUTO_EXEC_NAMES, ExtractedMacro, ExtractionOutcome

_SUB_PATTERN = re.compile(
    r"^[ \t]*(?:Public|Private|Friend)?[ \t]*Sub[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*\(([^)]*)\)",
    re.IGNORECASE | re.MULTILINE,
)
_END_SUB_PATTERN = re.compile(r"^[ \t]*End[ \t]+Sub[ \t]*$", re.IGNORECASE | re.MULTILINE)


def _split_subs(module_source: str) -> list[tuple[str, str, bool]]:
    """Returns (proc_name, full_source_of_the_sub, has_parameters) for every
    ``Sub``/``End Sub`` block in a module's source. Anything outside a
    Sub/End Sub pair (module-level Dim, Functions, comments) is not a
    separately runnable unit and is not returned -- Virtual Run v1 only
    lists/executes Subs."""
    results: list[tuple[str, str, bool]] = []
    for match in _SUB_PATTERN.finditer(module_source):
        proc_name = match.group(1)
        params = match.group(2).strip()
        end_match = _END_SUB_PATTERN.search(module_source, match.end())
        if not end_match:
            continue
        full_source = module_source[match.start():end_match.end()]
        results.append((proc_name, full_source, bool(params)))
    return results


def extract_macros(workbook_bytes: bytes) -> ExtractionOutcome:
    """Extract every Sub procedure from every VBA module in an .xlsm's
    vbaProject.bin. Never executes anything -- this only reads and parses
    source text."""
    try:
        parser = VBA_Parser(filename="workbook.xlsm", data=workbook_bytes)
        macros: list[ExtractedMacro] = []
        for _container, _stream_path, vba_filename, vba_code in parser.extract_macros():
            module_name = vba_filename.rsplit(".", 1)[0] if vba_filename else "Module"
            for proc_name, sub_source, has_parameters in _split_subs(vba_code):
                macros.append(ExtractedMacro(
                    module_name=module_name,
                    proc_name=proc_name,
                    source=sub_source,
                    is_auto_exec=proc_name.lower() in AUTO_EXEC_NAMES,
                    has_parameters=has_parameters,
                ))
        return ExtractionOutcome(macros=macros)
    except Exception as exc:  # noqa: BLE001 -- surfaced as a failed extraction run, never raised further
        return ExtractionOutcome(error_message=str(exc))
    finally:
        try:
            parser.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass
