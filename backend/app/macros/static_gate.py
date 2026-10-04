"""Static safety classification for an extracted macro.

Three independent checks, always in this order:

1. **Dangerous-keyword scan** (`oletools.olevba.VBA_Scanner`, real analysis,
   not a guess): anything reaching outside the workbook -- Shell, file/
   registry/network APIs, other applications -- is classified
   `BLOCKED_EXTERNAL`, permanently, regardless of whether the rest of the
   macro would otherwise parse or run. This check runs even on macros the
   parser can't make sense of, so a dangerous macro is never accidentally
   let through just because it also happens to be syntactically unusual.
   The one narrow exemption is `CreateObject("Scripting.Dictionary")`
   (see `_only_safe_create_object_calls` below) -- without it, the
   scanner's blanket "CreateObject is Suspicious" finding would block
   every Dictionary-aggregation macro before the interpreter lane (which
   exists specifically to run that pattern) is ever reached, making that
   whole capability dead code in practice.
2. **Parse + SQL fast-lane eligibility** (only reached if step 1 passes):
   the macro is parsed into an AST (`parser.statement_parser`) and checked
   against the SQL fast lane's narrow shape (`sql_lane.is_eligible`). A
   macro that fits is `RUNNABLE`/`SQL` -- no per-row execution, one bulk
   SQL round trip.
3. **General-interpreter safety check** (only reached if step 2 doesn't
   fit): `interpreter.check_safety` name-checks every identifier/property/
   method the AST references against the closed-world virtual workbook's
   exact surface (`interpreter.py`'s module docstring), and separately
   re-validates every `CreateObject` argument via the real AST -- so the
   keyword-scan exemption above is a narrow, regex-verified pre-filter,
   never the sole safety check for what actually gets to run.
   A macro that passes is `RUNNABLE`/`INTERPRETED`. Anything that fails to
   parse, or parses but neither lane accepts, is `BLOCKED_UNSUPPORTED`
   with the precise reason -- never approximated, never silently widened.
"""

from __future__ import annotations

import re

from oletools.olevba import VBA_Scanner

from . import interpreter, sql_lane
from .models import BlockReason, ExtractedMacro
from .parser.statement_parser import ParseError, parse_sub

#: oletools keyword categories that indicate the macro reaches outside the
#: workbook. ("AutoExec" and generic string/pattern categories are not
#: included here -- they describe trigger timing or incidental strings, not
#: an external-reaching capability by themselves.)
_EXTERNAL_KEYWORD_TYPES = {"Suspicious", "IOC", "Hex String", "Base64 String", "Dridex string"}

_CREATE_OBJECT_CALL_RE = re.compile(r'CreateObject\s*\(\s*"([^"]*)"\s*\)', re.IGNORECASE)
_CREATE_OBJECT_ANY_CALL_RE = re.compile(r"CreateObject\s*\(", re.IGNORECASE)


def _only_safe_create_object_calls(source: str) -> bool:
    """True if every ``CreateObject(...)`` call in `source` is the literal,
    explicitly-supported ``CreateObject("Scripting.Dictionary")`` pattern --
    the exact one `interpreter.check_safety` allows. Any OTHER target
    (FileSystemObject, WScript.Shell, Excel.Application, ...), or a call
    whose argument isn't a plain string literal at all (e.g. a variable),
    fails this and keeps the keyword scanner's BLOCKED_EXTERNAL finding --
    this is a narrow allowlist, not a general softening of the scan."""
    calls = _CREATE_OBJECT_ANY_CALL_RE.findall(source)
    if not calls:
        return True
    literal_targets = _CREATE_OBJECT_CALL_RE.findall(source)
    if len(literal_targets) != len(calls):
        return False
    return all(target.strip().lower() == "scripting.dictionary" for target in literal_targets)


def classify(macro: ExtractedMacro) -> tuple[str, list[BlockReason], str | None]:
    """Returns (static_risk, reasons, execution_lane). static_risk is one of
    'RUNNABLE' / 'BLOCKED_EXTERNAL' / 'BLOCKED_UNSUPPORTED'; execution_lane
    is 'SQL' / 'INTERPRETED' when RUNNABLE, else None."""
    scanner = VBA_Scanner(macro.source)
    findings = scanner.scan()
    exempt_create_object = _only_safe_create_object_calls(macro.source)
    external_reasons = [
        BlockReason(line=None, construct=keyword, reason=description)
        for keyword_type, keyword, description in findings
        if keyword_type in _EXTERNAL_KEYWORD_TYPES
        and not (keyword == "CreateObject" and exempt_create_object)
    ]
    if external_reasons:
        return "BLOCKED_EXTERNAL", external_reasons, None

    if macro.is_auto_exec:
        return "BLOCKED_UNSUPPORTED", [BlockReason(
            line=None, construct=macro.proc_name,
            reason="Event-triggered procedures (Auto_Open, Workbook_Open, ...) are not user-invoked and are never listed as runnable.",
        )], None
    if macro.has_parameters:
        return "BLOCKED_UNSUPPORTED", [BlockReason(
            line=None, construct=macro.proc_name,
            reason="Parameterized Subs are not supported in this release.",
        )], None

    try:
        sub_ast = parse_sub(macro.source)
    except ParseError as exc:
        return "BLOCKED_UNSUPPORTED", [BlockReason(line=exc.line, construct=macro.proc_name, reason=str(exc))], None

    eligible, sql_reason = sql_lane.is_eligible(sub_ast)
    if eligible:
        return "RUNNABLE", [], "SQL"

    safe, interpreter_reasons = interpreter.check_safety(sub_ast)
    if safe:
        return "RUNNABLE", [], "INTERPRETED"
    reason = "; ".join(interpreter_reasons) if interpreter_reasons else sql_reason
    return "BLOCKED_UNSUPPORTED", [BlockReason(line=None, construct=macro.proc_name, reason=reason)], None
