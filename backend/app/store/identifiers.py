"""SQL-identifier safety.

`validate_identifier()` is kept for existing call sites that only need a
boolean check. `SafeIdentifier` goes further: it validates in its
constructor and raises immediately on anything invalid, so a bare Python
str can never reach an f-string SQL call unvalidated — the check is a type
boundary, not a convention every call site has to remember to apply.
"""

import re

_IDENT_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def validate_identifier(name: str) -> bool:
    """Return True if `name` is a safe SQL identifier."""
    return bool(_IDENT_RE.match(name))


class SafeIdentifier(str):
    """A str subtype guaranteed to match `_IDENT_RE` at construction time.

    Behaves exactly like a str everywhere it's used (f-strings, dict keys,
    comparisons) since it subclasses str, so existing call sites that pass
    a SafeIdentifier where a str was expected need no changes. The only
    new constraint is that constructing one from an invalid value raises
    ValueError immediately, instead of letting an unvalidated identifier
    travel further into the code and reach a dynamic-SQL f-string.
    """

    def __new__(cls, value: str) -> "SafeIdentifier":
        if not validate_identifier(value):
            raise ValueError(
                f"Invalid SQL identifier: {value!r}. Must match {_IDENT_RE.pattern}"
            )
        return super().__new__(cls, value)
