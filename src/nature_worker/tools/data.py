"""Basic text and data processing tools."""

from __future__ import annotations

import ast
import csv
import io
import json
import math
import operator
import re
import statistics
from collections import Counter

from .workspace import ToolError, Workspace, _require

MAX_SEARCH_RESULTS = 100
MAX_SEARCH_FILE_BYTES = 2_000_000


def search_text(ws: Workspace, args: dict) -> str:
    query = _require(args, "query")
    use_regex = bool(args.get("regex", False))
    try:
        pattern = re.compile(query if use_regex else re.escape(query), re.IGNORECASE)
    except re.error as e:
        raise ToolError(f"Invalid regular expression: {e}") from None

    results: list[str] = []
    for entry in ws.list(args.get("path") or ".", recursive=True):
        if entry["type"] != "file" or (entry["size"] or 0) > MAX_SEARCH_FILE_BYTES:
            continue
        try:
            text = ws.read_text(entry["path"])
        except ToolError:
            continue  # binary or unreadable
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                results.append(f"{entry['path']}:{lineno}: {line.strip()[:200]}")
                if len(results) >= MAX_SEARCH_RESULTS:
                    results.append(f"... stopped after {MAX_SEARCH_RESULTS} matches")
                    return "\n".join(results)
    return "\n".join(results) if results else "No matches found."


def _load_rows(ws: Workspace, path: str) -> list[dict]:
    text = ws.read_text(path)
    lower = path.lower()
    if lower.endswith(".json"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ToolError(f"Invalid JSON: {e}") from None
        if isinstance(data, dict):
            # Accept {"items": [...]}-style documents with a single list value.
            lists = [v for v in data.values() if isinstance(v, list)]
            data = lists[0] if len(lists) == 1 else [data]
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise ToolError("JSON must be a list of objects to summarize as a table.")
        return data
    if lower.endswith((".csv", ".tsv", ".txt")):
        delimiter = "\t" if lower.endswith(".tsv") else None
        if delimiter is None:
            try:
                delimiter = csv.Sniffer().sniff(text[:5000], delimiters=",;\t|").delimiter
            except csv.Error:
                delimiter = ","
        return list(csv.DictReader(io.StringIO(text), delimiter=delimiter))
    raise ToolError("Supported table formats: .csv, .tsv, .json")


def _number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def table_summary(ws: Workspace, args: dict) -> str:
    path = _require(args, "path")
    rows = _load_rows(ws, path)
    if not rows:
        return "The table has no rows."
    columns: list[str] = []
    for r in rows:
        for k in r:
            if k not in columns:
                columns.append(k)
    lines = [f"Rows: {len(rows)}", f"Columns ({len(columns)}): {', '.join(map(str, columns))}", ""]
    for col in columns:
        values = [r.get(col) for r in rows if r.get(col) not in (None, "")]
        nums = [n for n in (_number(v) for v in values) if n is not None]
        missing = len(rows) - len(values)
        if values and len(nums) == len(values):
            lines.append(
                f"- {col}: numeric, count={len(nums)}, missing={missing}, sum={sum(nums):g}, "
                f"min={min(nums):g}, max={max(nums):g}, mean={statistics.fmean(nums):.4g}, "
                f"median={statistics.median(nums):g}"
            )
        else:
            counts = Counter(str(v) for v in values)
            top = ", ".join(f"{v!r} ({c})" for v, c in counts.most_common(5))
            lines.append(f"- {col}: text, distinct={len(counts)}, missing={missing}, top: {top}")
    return "\n".join(lines)


_BIN_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {
    "abs": abs, "round": round, "min": min, "max": max, "sum": lambda *a: sum(a),
    "sqrt": math.sqrt, "log": math.log, "log10": math.log10, "exp": math.exp,
    "floor": math.floor, "ceil": math.ceil,
}
_CONSTS = {"pi": math.pi, "e": math.e}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ToolError("Exponent too large.")
        return _BIN_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
        return _FUNCS[node.func.id](*[_eval(a) for a in node.args])
    raise ToolError("Unsupported expression. Use numbers, + - * / // % **, parentheses and basic math functions.")


def calculate(ws: Workspace, args: dict) -> str:
    expr = _require(args, "expression")
    if len(expr) > 500:
        raise ToolError("Expression too long.")
    try:
        result = _eval(ast.parse(expr, mode="eval"))
    except SyntaxError:
        raise ToolError("Invalid expression syntax.") from None
    except (ZeroDivisionError, ValueError, OverflowError, TypeError) as e:
        raise ToolError(f"Calculation error: {e}") from None
    if isinstance(result, float):
        result = float(f"{result:.12g}")  # hide binary floating-point noise (0.1+0.2 -> 0.3)
    return f"{expr} = {result}"
