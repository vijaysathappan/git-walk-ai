"""Precedence parser that converts Excel tokens into a deterministic AST."""

from .ast import ASTNode
from .tokenizer import FormulaToken, tokenize


class FormulaParseError(ValueError):
    pass


PRECEDENCE = {
    "=": 1, "<>": 1, "<": 1, ">": 1, "<=": 1, ">=": 1,
    "&": 2, "+": 3, "-": 3, "*": 4, "/": 4, "^": 5,
}


class FormulaParser:
    version = "formula-parser@1.0"

    def parse(self, formula: str) -> ASTNode:
        self.tokens = tokenize(formula)
        self.position = 0
        if not self.tokens:
            raise FormulaParseError("Formula is empty")
        node = self._expression(0)
        if self.position < len(self.tokens):
            remaining = "".join(token.value for token in self.tokens[self.position:])
            raise FormulaParseError(f"Unsupported formula tail: {remaining}")
        return node

    def _peek(self) -> FormulaToken | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def _take(self) -> FormulaToken:
        token = self._peek()
        if token is None:
            raise FormulaParseError("Unexpected end of formula")
        self.position += 1
        return token

    def _expression(self, minimum: int) -> ASTNode:
        left = self._primary()
        while True:
            token = self._peek()
            if not token or token.type != "OP_IN":
                break
            precedence = PRECEDENCE.get(token.value, 0)
            if precedence < minimum:
                break
            operator = self._take().value
            right = self._expression(precedence + (0 if operator == "^" else 1))
            left = ASTNode("binary_operator", operator, (left, right))
        token = self._peek()
        if token and token.type == "OP_POST":
            left = ASTNode("postfix_operator", self._take().value, (left,))
        return left

    def _primary(self) -> ASTNode:
        token = self._take()
        if token.type == "OP_PRE":
            return ASTNode("unary_operator", token.value, (self._primary(),))
        if token.type == "OPERAND":
            kind = {
                "RANGE": "reference", "NUMBER": "number", "TEXT": "text",
                "LOGICAL": "logical", "ERROR": "error",
            }.get(token.subtype, "literal")
            return ASTNode(kind, token.value, subtype=token.subtype or None)
        if token.type == "FUNC" and token.subtype == "OPEN":
            arguments = []
            while True:
                closing = self._peek()
                if closing is None:
                    raise FormulaParseError(f"Function {token.value} is not closed")
                if closing.type == "FUNC" and closing.subtype == "CLOSE":
                    self._take()
                    break
                arguments.append(self._expression(0))
                separator = self._peek()
                if separator and separator.type == "SEP":
                    self._take()
                    continue
                if separator and separator.type == "FUNC" and separator.subtype == "CLOSE":
                    continue
                raise FormulaParseError(f"Unexpected token in {token.value}: {separator}")
            return ASTNode("function", token.value.rstrip("("), tuple(arguments))
        if token.type == "PAREN" and token.subtype == "OPEN":
            child = self._expression(0)
            closing = self._take()
            if closing.type != "PAREN" or closing.subtype != "CLOSE":
                raise FormulaParseError("Parenthesized expression is not closed")
            return ASTNode("group", children=(child,))
        raise FormulaParseError(f"Unsupported token: {token.type}/{token.subtype} {token.value}")
