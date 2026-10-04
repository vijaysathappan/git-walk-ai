"""Excel-aware token stream backed by openpyxl rather than regex tokenization."""

from dataclasses import dataclass

from openpyxl.formula import Tokenizer


@dataclass(frozen=True)
class FormulaToken:
    value: str
    type: str
    subtype: str


def tokenize(formula: str) -> list[FormulaToken]:
    source = formula if formula.startswith("=") else f"={formula}"
    type_names = {
        "OPERATOR-INFIX": "OP_IN",
        "OPERATOR-PREFIX": "OP_PRE",
        "OPERATOR-POSTFIX": "OP_POST",
    }
    return [
        FormulaToken(item.value, type_names.get(item.type, item.type), item.subtype)
        for item in Tokenizer(source).items
        if item.type != "WSPACE"
    ]
