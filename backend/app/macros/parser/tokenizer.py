"""Line-oriented VBA statement tokenizer. Line continuations (` _` at end of
line) are collapsed first via oletools' own helper, so token positions
still map back to sensible source lines for error reporting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from oletools.olevba import vba_collapse_long_lines

KEYWORDS = {
    "sub", "end", "dim", "as", "for", "to", "step", "next", "each", "in",
    "public", "private", "friend", "set", "call",
    "mod", "and", "or", "not", "true", "false",
    "if", "then", "elseif", "else",
    "do", "while", "until", "loop",
    "exit", "on", "error", "resume", "goto",
}

_TOKEN_RE = re.compile(r"""
      (?P<STRING>"(?:[^"]|"")*")
    | (?P<NUMBER>\d+\.\d+|\d+)
    | (?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<LE><=) | (?P<GE>>=) | (?P<NE><>)
    | (?P<OP>[+\-*/^=<>&\\])
    | (?P<LPAREN>\() | (?P<RPAREN>\))
    | (?P<COMMA>,) | (?P<DOT>\.) | (?P<COLON>:)
    | (?P<COMMENT>'.*$)
    | (?P<SKIP>[ \t]+)
    | (?P<OTHER>.)
""", re.VERBOSE)


@dataclass(frozen=True)
class Token:
    type: str
    value: str
    line: int


class TokenizeError(ValueError):
    pass


def tokenize(source: str) -> list[Token]:
    source = vba_collapse_long_lines(source)
    tokens: list[Token] = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        found_any = False
        for match in _TOKEN_RE.finditer(line):
            kind = match.lastgroup
            value = match.group()
            if kind in ("SKIP", "COMMENT"):
                continue
            if kind == "OTHER":
                raise TokenizeError(f"line {line_number}: unrecognized character {value!r}")
            if kind == "IDENT" and value.lower() in KEYWORDS:
                kind = value.lower()
            # ':' separates multiple statements on one physical line -- the
            # statement parser only needs to know "a statement ended here",
            # same as NEWLINE, so it's emitted as one.
            tokens.append(Token("NEWLINE", "\n", line_number) if kind == "COLON" else Token(kind, value, line_number))
            found_any = True
        if found_any:
            tokens.append(Token("NEWLINE", "\n", line_number))
    tokens.append(Token("EOF", "", tokens[-1].line if tokens else 0))
    return tokens
