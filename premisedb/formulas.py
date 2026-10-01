"""Formulas on model Variables: a small arithmetic language over the keys of Variables in one Model.

A formula is a Python expression restricted to numbers, Variable keys, + - * / **, unary minus,
comparisons, `a if cond else b`, and the functions min, max, sum, abs and prev. A value is a number
or a list aligned with the Variable's `periods`; lists combine element-wise, scalars broadcast, and a
`null` period yields `null`. `prev(x)` shifts a list one period later, `prev(x, start)` fills the first.
"""

import ast
from dataclasses import dataclass
import math

FUNCTIONS = ("min", "max", "sum", "abs", "prev")
_OPERATORS = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b, ast.Pow: lambda a, b: a ** b,
    ast.Lt: lambda a, b: a < b, ast.LtE: lambda a, b: a <= b, ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b, ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b,
}


@dataclass(frozen=True)
class VariableCheck:
    key: str
    stored: object  # The Variable's current value.
    computed: object  # None when the formula could not be evaluated.
    matches: bool
    problems: tuple[str, ...]  # Why it could not be evaluated or does not match.


@dataclass(frozen=True)
class ModelCheck:
    model_id: str
    inputs: int
    outputs: int
    checked: tuple[VariableCheck, ...]  # Outputs with a formula, in dependency order.
    without_formula: tuple[str, ...]  # Output keys that cannot be checked.


def parse(formula: str) -> tuple[ast.Expression, frozenset[str]]:
    """Parse a formula and name the Variables it uses; anything outside the language raises ValueError."""
    if not isinstance(formula, str) or not formula.strip():
        raise ValueError("formula must be a nonblank string")
    try:
        tree = ast.parse(formula.strip(), mode="eval")
    except SyntaxError as error:
        raise ValueError(f"formula syntax: {error.msg}") from None
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id not in FUNCTIONS:
                names.add(node.id)
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS or node.keywords:
                raise ValueError("formula calls must be min, max, sum, abs or prev with positional arguments")
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError("formula constants must be numbers")
        elif isinstance(node, (ast.BinOp, ast.UnaryOp, ast.Compare)):
            ops = node.ops if isinstance(node, ast.Compare) else [node.op]
            if not all(type(op) in _OPERATORS or isinstance(op, (ast.USub, ast.UAdd)) for op in ops):
                raise ValueError("formula operators are + - * / ** and comparisons")
        elif not isinstance(node, (ast.Expression, ast.IfExp, ast.Load, *tuple(_OPERATORS), ast.USub, ast.UAdd)):
            raise ValueError(f"formula cannot contain {type(node).__name__}")
    return tree, frozenset(names)


def evaluate(formula: str, values: dict[str, object]) -> object:
    """Evaluate a formula over Variable values; lists are element-wise with `null` propagating."""
    tree, names = parse(formula)
    missing = sorted(names - set(values))
    if missing:
        raise ValueError(f"formula names unknown Variables: {', '.join(missing)}")
    return _node(tree.body, values)


def _node(node, values):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return _check_value(node.id, values[node.id])
    if isinstance(node, ast.UnaryOp):
        return _map(lambda a: -a if isinstance(node.op, ast.USub) else a, _node(node.operand, values))
    if isinstance(node, ast.BinOp):
        return _map(_OPERATORS[type(node.op)], _node(node.left, values), _node(node.right, values))
    if isinstance(node, ast.Compare):
        left, result = _node(node.left, values), None
        for op, comparator in zip(node.ops, node.comparators):
            right = _node(comparator, values)
            term = _map(_OPERATORS[type(op)], left, right)
            result = term if result is None else _map(lambda a, b: a and b, result, term)
            left = right
        return result
    if isinstance(node, ast.IfExp):
        return _map(lambda c, a, b: a if c else b, _node(node.test, values), _node(node.body, values),
                    _node(node.orelse, values))
    if isinstance(node, ast.Call):
        args = [_node(arg, values) for arg in node.args]
        if node.func.id == "prev":
            if not 1 <= len(args) <= 2 or not isinstance(args[0], list):
                raise ValueError("prev takes a list and an optional starting value")
            return [args[1] if len(args) == 2 else None] + args[0][:-1]
        if node.func.id == "abs":
            return _map(abs, *args)
        function = {"min": min, "max": max, "sum": sum}[node.func.id]
        if len(args) == 1 and isinstance(args[0], list):  # Aggregate one list across its periods.
            items = [a for a in args[0] if a is not None]
            return function(items) if items else None
        return _map(lambda *a: function(a), *args)
    raise ValueError(f"formula cannot contain {type(node).__name__}")


def _check_value(key, value):
    if isinstance(value, bool) or not isinstance(value, (int, float, list)) or (
            isinstance(value, list) and not all(v is None or (isinstance(v, (int, float)) and not isinstance(v, bool))
                                                for v in value)):
        raise ValueError(f"Variable {key} has no numeric value or list of values")
    return value


def _map(function, *operands):
    """Apply element-wise over lists of equal length, broadcasting scalars; a None operand yields None."""
    lists = [o for o in operands if isinstance(o, list)]
    if not lists:
        return None if any(o is None for o in operands) else _finite(function(*operands))
    if len({len(l) for l in lists}) != 1:
        raise ValueError("formula combines lists with different numbers of periods")
    return [_map(function, *(o[i] if isinstance(o, list) else o for o in operands)) for i in range(len(lists[0]))]


def _finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("formula produced an infinite or undefined value")
    return value


def matches(stored, computed, *, tolerance: float) -> bool:
    """True when every stored number is within `tolerance` (relative, or absolute below 1) of the computed one."""
    if isinstance(stored, list) or isinstance(computed, list):
        return (isinstance(stored, list) and isinstance(computed, list) and len(stored) == len(computed)
                and all(matches(s, c, tolerance=tolerance) for s, c in zip(stored, computed)))
    if stored is None or computed is None:
        return stored is None and computed is None
    if isinstance(stored, bool) or not isinstance(stored, (int, float)):
        return False
    return abs(stored - computed) <= tolerance * max(1.0, abs(stored))


def check_model(variables: list[dict[str, object]], model_id: str, *, tolerance: float = 1e-4) -> ModelCheck:
    """Evaluate every output formula over the model's stored values and compare with the stored result.

    `variables` holds each Variable's attributes plus its `id` and its `depends_on` keys. Each formula is
    checked on its own, so one wrong output does not make its dependents wrong too. A formula must name
    only Variables of the same model that the output also `depends_on`.
    """
    if not isinstance(tolerance, (int, float)) or isinstance(tolerance, bool) or not 0 <= tolerance < 1:
        raise ValueError("tolerance must be a number from 0 to less than 1")
    by_key = {}
    for variable in variables:
        key = variable.get("key")
        if not isinstance(key, str) or not key:
            raise ValueError(f"Variable {variable.get('id')} has no key attribute")
        if key in by_key:
            raise ValueError(f"two Variables in the model share the key {key!r}")
        by_key[key] = variable
    values = {key: v.get("value") for key, v in by_key.items()}
    outputs = [v for v in by_key.values() if v.get("role") == "output"]
    checked = []
    for variable in outputs:
        if variable.get("formula") is None:
            continue
        key, stored = variable["key"], variable.get("value")
        try:
            _, names = parse(variable["formula"])
            unknown = sorted(names - set(by_key))
            undeclared = sorted(names - set(variable.get("depends_on", ())) - set(unknown) - {key})
            problems = ([f"names Variables not in the model: {', '.join(unknown)}"] if unknown else []) + (
                [f"names Variables without a depends_on link: {', '.join(undeclared)}"] if undeclared else [])
            if key in names:
                problems.append("formula refers to its own Variable")
            if problems:
                checked.append(VariableCheck(key, stored, None, False, tuple(problems)))
                continue
            computed = evaluate(variable["formula"], values)
        except (ValueError, ZeroDivisionError, TypeError) as error:
            checked.append(VariableCheck(key, stored, None, False, (str(error),)))
            continue
        ok = matches(stored, computed, tolerance=tolerance)
        checked.append(VariableCheck(key, stored, computed, ok,
                                     () if ok else ("stored value differs from the formula's result",)))
    return ModelCheck(model_id, sum(v.get("role") == "input" for v in by_key.values()), len(outputs),
                      tuple(checked), tuple(v["key"] for v in outputs if v.get("formula") is None))
