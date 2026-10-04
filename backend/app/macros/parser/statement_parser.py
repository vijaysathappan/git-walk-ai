"""Recursive-descent parser: tokens -> SubDecl AST, for the supported VBA
subset only. Anything else raises ParseError with a line number, which
callers turn into a precise BLOCKED_UNSUPPORTED reason -- never guessed at.

Expressions parse into a general postfix chain (see ast.py's module
docstring): one grammar covers cell references, sheet/collection lookups,
and method calls alike, rather than special-casing ``Cells(...)``.
"""

from __future__ import annotations

from . import ast as A
from .tokenizer import Token, tokenize

_COMPARE_OPS = {"=", "<>", "<", ">", "<=", ">="}


class ParseError(ValueError):
    def __init__(self, message: str, line: int):
        self.line = line
        super().__init__(f"line {line}: {message}")


class _Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0

    def _peek(self) -> Token:
        return self.tokens[self.pos]

    def _at(self, *types: str) -> bool:
        return self._peek().type in types

    def _advance(self) -> Token:
        token = self.tokens[self.pos]
        if token.type != "EOF":
            self.pos += 1
        return token

    def _expect(self, type_: str) -> Token:
        token = self._peek()
        if token.type != type_:
            raise ParseError(f"expected {type_}, found {token.type} {token.value!r}", token.line)
        return self._advance()

    def _skip_newlines(self) -> None:
        while self._at("NEWLINE"):
            self._advance()

    # ---- top level ----------------------------------------------------

    def parse_sub(self) -> A.SubDecl:
        self._skip_newlines()
        if self._at("public", "private", "friend"):
            self._advance()
        self._expect("sub")
        name_token = self._expect("IDENT")
        self._expect("LPAREN")
        if not self._at("RPAREN"):
            raise ParseError("parameterized Subs are not supported", name_token.line)
        self._expect("RPAREN")
        self._skip_newlines()
        body = self._parse_stmts(stop_at={"end"})
        self._expect("end")
        self._expect("sub")
        self._skip_newlines()
        if not self._at("EOF"):
            raise ParseError(f"unexpected content after End Sub: {self._peek().value!r}", self._peek().line)
        return A.SubDecl(name=name_token.value, body=body)

    def _parse_stmts(self, stop_at: set[str]) -> list[A.Stmt]:
        stmts: list[A.Stmt] = []
        self._skip_newlines()
        while not self._at(*stop_at, "EOF"):
            stmts.append(self._parse_stmt())
            self._skip_newlines()
        return stmts

    def _parse_stmt(self) -> A.Stmt:
        token = self._peek()
        if token.type == "dim":
            return self._parse_dim()
        if token.type == "for":
            return self._parse_for()
        if token.type == "do":
            return self._parse_do_loop()
        if token.type == "if":
            return self._parse_if()
        if token.type == "exit":
            return self._parse_exit()
        if token.type == "on":
            return self._parse_on_error()
        if token.type == "call":
            self._advance()  # `Call Foo` / `Call Foo(args)` -- optional keyword form of a statement call
            return self._parse_assign_or_call()
        return self._parse_assign_or_call()

    def _parse_dim(self) -> A.DimStmt:
        self._expect("dim")
        names = [self._parse_one_dim_var()]
        while self._at("COMMA"):
            self._advance()
            names.append(self._parse_one_dim_var())
        self._expect("NEWLINE")
        return A.DimStmt(names=names, type_name=None)

    def _parse_one_dim_var(self) -> str:
        name = self._expect("IDENT").value
        if self._at("as"):
            self._advance()
            self._expect("IDENT")  # type name -- discarded, the interpreter is dynamically typed
        return name

    def _parse_for(self) -> A.Stmt:
        line = self._peek().line
        self._expect("for")
        if self._at("each"):
            self._advance()
            var = self._expect("IDENT").value
            self._expect("in")
            iterable = self._parse_expr()
            self._expect("NEWLINE")
            body = self._parse_stmts(stop_at={"next"})
            self._expect("next")
            if self._at("IDENT"):
                self._advance()
            self._expect("NEWLINE")
            return A.ForEachStmt(var=var, iterable=iterable, body=body, line=line)

        var = self._expect("IDENT").value
        self._expect_op("=")
        start = self._parse_expr()
        self._expect("to")
        end = self._parse_expr()
        step = None
        if self._at("step"):
            self._advance()
            step = self._parse_expr()
        self._expect("NEWLINE")
        body = self._parse_stmts(stop_at={"next"})
        self._expect("next")
        if self._at("IDENT"):
            self._advance()
        self._expect("NEWLINE")
        return A.ForStmt(var=var, start=start, end=end, step=step, body=body, line=line)

    def _parse_do_loop(self) -> A.DoLoopStmt:
        line = self._peek().line
        self._expect("do")
        if self._at("while"):
            self._advance()
            negate = False
        elif self._at("until"):
            self._advance()
            negate = True
        else:
            raise ParseError("'Do' must be followed by 'While' or 'Until' (post-test Do...Loop While/Until is not supported)", line)
        condition = self._parse_expr()
        self._expect("NEWLINE")
        body = self._parse_stmts(stop_at={"loop"})
        self._expect("loop")
        self._expect("NEWLINE")
        return A.DoLoopStmt(condition=condition, negate=negate, body=body, line=line)

    def _parse_if(self) -> A.IfStmt:
        line = self._peek().line
        branches: list[A.IfBranch] = []
        self._expect("if")
        condition = self._parse_expr()
        self._expect("then")
        self._expect("NEWLINE")
        body = self._parse_stmts(stop_at={"elseif", "else", "end"})
        branches.append(A.IfBranch(condition, body))
        while self._at("elseif"):
            self._advance()
            condition = self._parse_expr()
            self._expect("then")
            self._expect("NEWLINE")
            body = self._parse_stmts(stop_at={"elseif", "else", "end"})
            branches.append(A.IfBranch(condition, body))
        if self._at("else"):
            self._advance()
            self._expect("NEWLINE")
            body = self._parse_stmts(stop_at={"end"})
            branches.append(A.IfBranch(None, body))
        self._expect("end")
        self._expect("if")
        self._expect("NEWLINE")
        return A.IfStmt(branches=branches, line=line)

    def _parse_exit(self) -> A.ExitStmt:
        line = self._peek().line
        self._expect("exit")
        if self._at("for"):
            self._advance()
            kind = "For"
        elif self._at("do"):
            self._advance()
            kind = "Do"
        elif self._at("sub"):
            self._advance()
            kind = "Sub"
        else:
            raise ParseError("'Exit' must be followed by For, Do, or Sub", line)
        self._expect("NEWLINE")
        return A.ExitStmt(kind=kind, line=line)

    def _parse_on_error(self) -> A.OnErrorStmt:
        line = self._peek().line
        self._expect("on")
        self._expect("error")
        if self._at("resume"):
            self._advance()
            self._expect("next")
            mode = "RESUME_NEXT"
        elif self._at("goto"):
            self._advance()
            zero = self._expect("NUMBER")
            if zero.value != "0":
                raise ParseError("only 'On Error GoTo 0' is supported (not a line label)", line)
            mode = "GOTO_0"
        else:
            raise ParseError("only 'On Error Resume Next' and 'On Error GoTo 0' are supported", line)
        self._expect("NEWLINE")
        return A.OnErrorStmt(mode=mode, line=line)

    def _parse_assign_or_call(self) -> A.Stmt:
        line = self._peek().line
        if self._at("set"):
            self._advance()  # Set vs plain assignment: no semantic difference for this interpreter
        target = self._parse_postfix()
        if self._at("OP") and self._peek().value == "=":
            self._advance()
            value = self._parse_expr()
            self._expect("NEWLINE")
            return A.AssignStmt(target=target, value=value, line=line)
        if self._at("NEWLINE"):
            self._advance()
            return A.CallStmt(call=target, line=line)
        # Bare-args statement call, e.g. `totals.Add category, amount` (no parens).
        args = [self._parse_expr()]
        while self._at("COMMA"):
            self._advance()
            args.append(self._parse_expr())
        self._expect("NEWLINE")
        return A.CallStmt(call=A.Call(base=target, args=args), line=line)

    def _expect_op(self, value: str) -> None:
        token = self._peek()
        if not (token.type == "OP" and token.value == value):
            raise ParseError(f"expected {value!r}, found {token.value!r}", token.line)
        self._advance()

    # ---- expressions (precedence climbing) -----------------------------

    def _parse_expr(self) -> A.Expr:
        return self._parse_or()

    def _parse_or(self) -> A.Expr:
        left = self._parse_and()
        while self._at("or"):
            self._advance()
            left = A.BinOp("Or", left, self._parse_and())
        return left

    def _parse_and(self) -> A.Expr:
        left = self._parse_not()
        while self._at("and"):
            self._advance()
            left = A.BinOp("And", left, self._parse_not())
        return left

    def _parse_not(self) -> A.Expr:
        if self._at("not"):
            self._advance()
            return A.UnaryOp("Not", self._parse_not())
        return self._parse_compare()

    def _parse_compare(self) -> A.Expr:
        left = self._parse_concat()
        while self._at("OP", "LE", "GE", "NE") and self._peek().value in _COMPARE_OPS:
            op = self._advance().value
            left = A.BinOp(op, left, self._parse_concat())
        return left

    def _parse_concat(self) -> A.Expr:
        left = self._parse_add()
        while self._at("OP") and self._peek().value == "&":
            self._advance()
            left = A.BinOp("&", left, self._parse_add())
        return left

    def _parse_add(self) -> A.Expr:
        left = self._parse_mul()
        while self._at("OP") and self._peek().value in ("+", "-"):
            op = self._advance().value
            left = A.BinOp(op, left, self._parse_mul())
        return left

    def _parse_mul(self) -> A.Expr:
        left = self._parse_unary()
        while (self._at("OP") and self._peek().value in ("*", "/", "\\")) or self._at("mod"):
            op = self._advance().value
            left = A.BinOp(op.lower() if op.lower() == "mod" else op, left, self._parse_unary())
        return left

    def _parse_unary(self) -> A.Expr:
        if self._at("OP") and self._peek().value == "-":
            self._advance()
            return A.UnaryOp("-", self._parse_unary())
        return self._parse_pow()

    def _parse_pow(self) -> A.Expr:
        left = self._parse_postfix()
        if self._at("OP") and self._peek().value == "^":
            self._advance()
            return A.BinOp("^", left, self._parse_unary())
        return left

    def _parse_postfix(self) -> A.Expr:
        expr = self._parse_atom()
        while True:
            if self._at("DOT"):
                self._advance()
                name = self._expect("IDENT").value
                expr = A.MemberAccess(base=expr, name=name)
            elif self._at("LPAREN"):
                self._advance()
                args: list[A.Expr] = []
                if not self._at("RPAREN"):
                    args.append(self._parse_expr())
                    while self._at("COMMA"):
                        self._advance()
                        args.append(self._parse_expr())
                self._expect("RPAREN")
                expr = A.Call(base=expr, args=args)
            else:
                break
        return expr

    def _parse_atom(self) -> A.Expr:
        token = self._peek()
        if token.type == "NUMBER":
            self._advance()
            return A.Literal(float(token.value) if "." in token.value else int(token.value))
        if token.type == "STRING":
            self._advance()
            return A.Literal(token.value[1:-1].replace('""', '"'))
        if token.type == "true":
            self._advance()
            return A.Literal(True)
        if token.type == "false":
            self._advance()
            return A.Literal(False)
        if token.type == "LPAREN":
            self._advance()
            inner = self._parse_expr()
            self._expect("RPAREN")
            return inner
        if token.type == "IDENT":
            self._advance()
            return A.Identifier(name=token.value)
        raise ParseError(f"unexpected token {token.value!r}", token.line)


def parse_sub(source: str) -> A.SubDecl:
    return _Parser(tokenize(source)).parse_sub()
