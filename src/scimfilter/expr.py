"""SCIM 2.0 filter expression parsing, evaluation and SQL generation.

A filter text is tokenized into ``(kind, text)`` pairs, turned into a small
tuple based syntax tree, and can then be evaluated against a directory
resource or compiled into a parameterised SQL predicate.
"""

from __future__ import annotations

import re
from typing import Any, List, Mapping, Optional, Sequence, Tuple

Token = Tuple[str, str]
Node = Tuple[Any, ...]

_COMPARISON_OPS = ("eq", "ne", "co", "sw", "ew", "gt", "ge", "lt", "le")
_LOGICAL_OPS = ("and", "or", "not")
_ALL_OPS = _COMPARISON_OPS + ("pr",) + _LOGICAL_OPS

_PATH_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")
_NUMBER_RE = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")

_ESCAPABLE = ('"', "\\")


def _read_string(text: str, start: int) -> Tuple[str, int]:
    """Read a quoted literal starting at ``start``."""
    chars: List[str] = []
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            if index + 1 >= len(text):
                raise ValueError("unterminated-string")
            escaped = text[index + 1]
            if escaped not in _ESCAPABLE:
                raise ValueError("invalid-escape")
            chars.append(escaped)
            index += 2
            continue
        if char == '"':
            return "".join(chars), index + 1
        chars.append(char)
        index += 1
    raise ValueError("unterminated-string")


def tokenize(text: str) -> List[Token]:
    """Split a filter text into tokens."""
    tokens: List[Token] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char == "(":
            tokens.append(("lparen", "("))
            index += 1
            continue
        if char == ")":
            tokens.append(("rparen", ")"))
            index += 1
            continue
        if char == '"':
            value, index = _read_string(text, index)
            tokens.append(("string", value))
            continue
        if char.isdigit() or (char == "-" and index + 1 < length and text[index + 1].isdigit()):
            match = _NUMBER_RE.match(text, index)
            if match is None:
                raise ValueError("unexpected-token")
            tokens.append(("number", match.group(0)))
            index = match.end()
            continue
        match = _PATH_RE.match(text, index)
        if match is None:
            raise ValueError("unexpected-token")
        word = match.group(0)
        index = match.end()
        lowered = word.lower()
        if lowered in _ALL_OPS:
            tokens.append(("op", lowered))
        elif lowered in ("true", "false"):
            tokens.append(("bool", lowered))
        elif lowered == "null":
            tokens.append(("null", lowered))
        else:
            tokens.append(("path", word))
    return tokens


def _peek(tokens: Sequence[Token], position: int) -> Optional[Token]:
    if position < 0 or position >= len(tokens):
        return None
    return tokens[position]


def _literal(token: Token) -> Any:
    kind, text = token
    if kind == "string":
        return text
    if kind == "number":
        if "." in text:
            return float(text)
        return int(text)
    if kind == "bool":
        return text == "true"
    if kind == "null":
        return None
    raise ValueError("unexpected-token")


def parse(text: str) -> Node:
    """Parse ``text`` into a tuple based syntax tree."""
    tokens = tokenize(text)
    node, position = _parse_or(tokens, 0)
    if position != len(tokens):
        raise ValueError("unexpected-token")
    return node


def _parse_or(tokens: Sequence[Token], position: int) -> Tuple[Node, int]:
    node, position = _parse_and(tokens, position)
    while _peek(tokens, position) == ("op", "or"):
        right, position = _parse_and(tokens, position + 1)
        node = ("or", node, right)
    return node, position


def _parse_and(tokens: Sequence[Token], position: int) -> Tuple[Node, int]:
    node, position = _parse_unary(tokens, position)
    while _peek(tokens, position) == ("op", "and"):
        right, position = _parse_unary(tokens, position + 1)
        node = ("and", node, right)
    return node, position


def _parse_unary(tokens: Sequence[Token], position: int) -> Tuple[Node, int]:
    if _peek(tokens, position) == ("op", "not"):
        node, position = _parse_unary(tokens, position + 1)
        return ("not", node), position
    return _parse_primary(tokens, position)


def _parse_primary(tokens: Sequence[Token], position: int) -> Tuple[Node, int]:
    token = _peek(tokens, position)
    if token is None:
        raise ValueError("unexpected-end")
    if token[0] == "lparen":
        node, position = _parse_or(tokens, position + 1)
        if _peek(tokens, position) != ("rparen", ")"):
            raise ValueError("unbalanced-paren")
        return node, position + 1
    if token[0] != "path":
        raise ValueError("unexpected-token")
    path = token[1]
    position += 1
    operator = _peek(tokens, position)
    if operator is None:
        raise ValueError("unexpected-end")
    if operator[0] != "op" or operator[1] in _LOGICAL_OPS:
        raise ValueError("unknown-operator")
    op = operator[1]
    position += 1
    if op == "pr":
        return ("compare", path, op, None), position
    literal_token = _peek(tokens, position)
    if literal_token is None:
        raise ValueError("unexpected-end")
    return ("compare", path, op, _literal(literal_token)), position + 1


def to_text(node: Node) -> str:
    """Render a syntax tree back into filter text."""
    return _render(node, 0)


def _render(node: Node, parent: int) -> str:
    kind = node[0]
    if kind == "compare":
        _tag, path, op, literal = node
        if op == "pr":
            return path + " pr"
        return path + " " + op + " " + _literal_text(literal)
    if kind == "not":
        return "not " + _render(node[1], 3)
    precedence = 1 if kind == "or" else 2
    left = _render(node[1], precedence)
    right = _render(node[2], precedence + 1)
    text = left + " " + kind + " " + right
    if precedence < parent:
        return "(" + text + ")"
    return text


def _literal_text(literal: Any) -> str:
    if literal is None:
        return "null"
    if literal is True:
        return "true"
    if literal is False:
        return "false"
    if isinstance(literal, str):
        escaped = literal.replace("\\", "\\\\").replace('"', '\\"')
        return '"' + escaped + '"'
    return repr(literal)


_MISSING = object()


def _lookup(resource: Mapping[str, Any], path: str) -> Tuple[bool, Any]:
    """Resolve a dotted attribute path, comparing names case-insensitively."""
    current: Any = resource
    for segment in path.split("."):
        if not isinstance(current, Mapping):
            return False, None
        found: Any = _MISSING
        for key, value in current.items():
            if isinstance(key, str) and key.lower() == segment.lower():
                found = value
                break
        if found is _MISSING:
            return False, None
        current = found
    return True, current


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _equal(value: Any, literal: Any) -> bool:
    if value is None or literal is None:
        return value is None and literal is None
    if isinstance(value, bool) or isinstance(literal, bool):
        return isinstance(value, bool) and isinstance(literal, bool) and value == literal
    if _is_number(value) and _is_number(literal):
        return float(value) == float(literal)
    if isinstance(value, str) and isinstance(literal, str):
        return value.lower() == literal.lower()
    return False


def _order(op: str, value: Any, literal: Any) -> bool:
    if _is_number(value) and _is_number(literal):
        left: Any = float(value)
        right: Any = float(literal)
    elif isinstance(value, str) and isinstance(literal, str):
        left = value.lower()
        right = literal.lower()
    else:
        return False
    if op == "gt":
        return left > right
    if op == "ge":
        return left >= right
    if op == "lt":
        return left < right
    return left <= right


def _compare(op: str, value: Any, literal: Any) -> bool:
    if isinstance(value, list):
        return any(_compare(op, item, literal) for item in value)
    if op == "eq":
        return _equal(value, literal)
    if op == "ne":
        return not _equal(value, literal)
    if op in ("co", "sw", "ew"):
        if not isinstance(value, str) or not isinstance(literal, str):
            return False
        left = value.lower()
        right = literal.lower()
        if op == "co":
            return right in left
        if op == "sw":
            return left.startswith(right)
        return left.endswith(right)
    if op in ("gt", "ge", "lt", "le"):
        return _order(op, value, literal)
    return False


def _evaluate_node(node: Node, resource: Mapping[str, Any]) -> bool:
    kind = node[0]
    if kind == "and":
        return _evaluate_node(node[1], resource) and _evaluate_node(node[2], resource)
    if kind == "or":
        return _evaluate_node(node[1], resource) or _evaluate_node(node[2], resource)
    if kind == "not":
        return not _evaluate_node(node[1], resource)
    _tag, path, op, literal = node
    present, value = _lookup(resource, path)
    if op == "pr":
        return present and value is not None
    if not present:
        return False
    return _compare(op, value, literal)


def evaluate(filter_text: str, resource: Mapping[str, Any]) -> bool:
    """Return whether ``resource`` satisfies ``filter_text``."""
    return _evaluate_node(parse(filter_text), resource)


def _column(path: str) -> str:
    return ".".join('"' + segment + '"' for segment in path.split("."))


def _sql(node: Node) -> Tuple[str, List[Any]]:
    kind = node[0]
    if kind in ("and", "or"):
        left_sql, left_params = _sql(node[1])
        right_sql, right_params = _sql(node[2])
        joiner = " AND " if kind == "and" else " OR "
        return "(" + left_sql + joiner + right_sql + ")", left_params + right_params
    if kind == "not":
        inner_sql, inner_params = _sql(node[1])
        return "NOT (" + inner_sql + ")", inner_params
    _tag, path, op, literal = node
    column = _column(path)
    if op == "pr":
        return column + " IS NOT NULL", []
    if op == "eq":
        return column + " = ?", [literal]
    if op == "ne":
        return column + " <> ?", [literal]
    if op == "co":
        return column + " LIKE ?", ["%" + literal + "%"]
    if op == "sw":
        return column + " LIKE ?", [literal + "%"]
    if op == "ew":
        return column + " LIKE ?", ["%" + literal]
    operator = {"gt": " > ", "ge": " >= ", "lt": " < ", "le": " <= "}[op]
    return column + operator + "?", [literal]


def to_sql_where(filter_text: str) -> Tuple[str, List[Any]]:
    """Compile ``filter_text`` into a SQL predicate and bound parameters."""
    return _sql(parse(filter_text))


def validate(text: str) -> List[str]:
    """Return the structural problems of ``text`` (empty when well formed)."""
    problems: List[str] = []
    depth = 0
    index = 0
    in_string = False
    while index < len(text):
        char = text[index]
        if in_string:
            if char == "\\":
                if index + 1 >= len(text):
                    problems.append("unterminated-string")
                    return problems
                if text[index + 1] not in _ESCAPABLE:
                    problems.append("invalid-escape")
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                problems.append("unbalanced-paren")
                depth = 0
        index += 1
    if in_string:
        problems.append("unterminated-string")
    if depth > 0:
        problems.append("unbalanced-paren")
    return problems