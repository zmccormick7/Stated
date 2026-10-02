"""Formulas on model Variables: a small arithmetic language over the keys of Variables in one Model.

A formula is a Python expression restricted to numbers, Variable keys, + - * / **, unary minus,
comparisons, `a if cond else b`, and the functions min, max, sum, abs, prev, at and fill. A value is a
number or a list labelled by the Variable's `periods`. Lists are aligned to the output's periods by
label, so rows over different years combine on the years they share; unlabelled lists align by
position. Scalars broadcast, and a `null` period yields `null`. `prev(x)` shifts a list one period
later and `prev(x, start)` fills the first; `at(x, "2027")` picks one period; `fill(x, v)` replaces
`null`. A formula may name its own Variable inside `prev` to define a period-by-period recurrence.
"""

import ast
from dataclasses import dataclass
import math

FUNCTIONS = ("min", "max", "sum", "abs", "prev", "next", "at", "fill")
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


@dataclass(frozen=True)
class VariableValue:
    key: str
    role: object
    stored: object  # The Variable's stored value.
    value: object  # Under the overrides: the override, the formula's result, or the stored value.
    changed: bool  # Whether the overrides moved it.


@dataclass(frozen=True)
class ModelEvaluation:
    model_id: str
    overrides: dict[str, object]
    variables: tuple[VariableValue, ...]  # Variables without a formula first, then formulas in dependency order.
    without_formula: tuple[str, ...]  # Output keys that keep their stored value because they have no formula.


def parse(formula: str) -> tuple[ast.Expression, frozenset[str]]:
    """Parse a formula and name the Variables it uses; anything outside the language raises ValueError."""
    if not isinstance(formula, str) or not formula.strip():
        raise ValueError("formula must be a nonblank string")
    try:
        tree = ast.parse(formula.strip(), mode="eval")
    except SyntaxError as error:
        raise ValueError(f"formula syntax: {error.msg}") from None
    names = set()
    labels = set()  # Period labels: the string second argument of at().
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id not in FUNCTIONS:
                names.add(node.id)
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS or node.keywords:
                raise ValueError("formula calls must be min, max, sum, abs, prev, next, at or fill with positional arguments")
            if node.func.id == "at":
                if len(node.args) != 2 or not isinstance(node.args[1], ast.Constant) \
                        or not isinstance(node.args[1].value, str):
                    raise ValueError('at takes a list and a period label, such as at(revenue, "2027")')
                labels.add(id(node.args[1]))
        elif isinstance(node, ast.Constant):
            if id(node) in labels:
                continue
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError("formula constants must be numbers")
        elif isinstance(node, (ast.BinOp, ast.UnaryOp, ast.Compare)):
            ops = node.ops if isinstance(node, ast.Compare) else [node.op]
            if not all(type(op) in _OPERATORS or isinstance(op, (ast.USub, ast.UAdd)) for op in ops):
                raise ValueError("formula operators are + - * / ** and comparisons")
        elif not isinstance(node, (ast.Expression, ast.IfExp, ast.Load, *tuple(_OPERATORS), ast.USub, ast.UAdd)):
            raise ValueError(f"formula cannot contain {type(node).__name__}")
    return tree, frozenset(names)


def evaluate(formula: str, values: dict[str, object], *, periods: dict[str, list] | None = None,
             target: list | None = None, self_key: str | None = None) -> object:
    """Evaluate a formula over Variable values.

    `periods` labels each list value and `target` labels the result. A labelled row keeps its own
    periods through prev, next, at and aggregates; when rows combine, each is read at the target's
    labels (`null` where a label is missing), or at identical labels when there is no target.
    Unlabelled lists combine by position and must have equal lengths. With `self_key`, the formula may
    name that Variable inside `prev(...)`, and the result is computed one period at a time.
    """
    tree, names = parse(formula)
    missing = sorted(names - set(values) - ({self_key} if self_key else set()))
    if missing:
        raise ValueError(f"formula names unknown Variables: {', '.join(missing)}")
    periods = periods or {}
    if target is not None and (not isinstance(target, list) or len(set(target)) != len(target)):
        raise ValueError("the output's periods must be a list of distinct labels")
    rows = {key: _row(key, value, periods.get(key)) for key, value in values.items()}
    if self_key not in names:
        return _plain(_node(tree.body, rows, target), target)
    # A recurrence: every reference to the output itself must sit inside prev(), and the result is a row.
    if target is None:
        raise ValueError("a formula that refers to its own Variable needs the output to have periods")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.func.id == "prev" and isinstance(node.args[0], ast.Name) \
                and node.args[0].id == self_key:
            node.args[0].id = _SELF
    if any(isinstance(node, ast.Name) and node.id == self_key for node in ast.walk(tree)):
        raise ValueError("a formula may refer to its own Variable only inside prev()")
    own = [None] * len(target)
    for index in range(len(target)):
        result = _plain(_node(tree.body, rows | {_SELF: _Row(target, list(own))}, target), target)
        if not isinstance(result, list):
            raise ValueError("a recurrence must produce a value for each period")
        own[index] = result[index]
    return own


_SELF = "\0self"  # Not a valid key, so it cannot collide with a Variable.


class _Row:
    """A list value with its period labels."""

    def __init__(self, labels, values):
        self.labels, self.values = labels, values

    def at(self, target):
        by_label = dict(zip(self.labels, self.values))
        return [by_label.get(label) for label in target]


def _row(key, value, labels):
    value = _check_value(key, value)
    if not isinstance(value, list) or labels is None:
        return value
    if not isinstance(labels, list) or len(labels) != len(value) or len(set(labels)) != len(labels):
        raise ValueError(f"Variable {key} has periods that do not match its values")
    return _Row(labels, value)


def _plain(value, target):
    """The result as a list or number; a labelled row is read at the target's labels."""
    if isinstance(value, _Row):
        return value.at(target) if target is not None else value.values
    return value


def _node(node, rows, target):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return rows[node.id]
    if isinstance(node, ast.UnaryOp):
        return _map(lambda a: -a if isinstance(node.op, ast.USub) else a, [_node(node.operand, rows, target)], target)
    if isinstance(node, ast.BinOp):
        return _map(_OPERATORS[type(node.op)], [_node(node.left, rows, target), _node(node.right, rows, target)], target)
    if isinstance(node, ast.Compare):
        left, result = _node(node.left, rows, target), None
        for op, comparator in zip(node.ops, node.comparators):
            right = _node(comparator, rows, target)
            term = _map(_OPERATORS[type(op)], [left, right], target)
            result = term if result is None else _map(lambda a, b: a and b, [result, term], target)
            left = right
        return result
    if isinstance(node, ast.IfExp):
        return _map(lambda c, a, b: a if c else b, [_node(part, rows, target) for part in
                                                   (node.test, node.body, node.orelse)], target)
    if isinstance(node, ast.Call):
        args = [_node(arg, rows, target) for arg in node.args]
        name = node.func.id
        if name in ("prev", "next"):
            if not 1 <= len(args) <= 2 or not isinstance(args[0], (list, _Row)):
                raise ValueError(f"{name} takes a list and an optional value for the period it cannot see")
            row, edge = args[0], args[1] if len(args) == 2 else None
            items = row.values if isinstance(row, _Row) else row
            shifted = [edge] + items[:-1] if name == "prev" else items[1:] + [edge]
            return _Row(row.labels, shifted) if isinstance(row, _Row) else shifted
        if name == "at":
            if not isinstance(args[0], _Row):
                raise ValueError("at needs a list whose periods are known")
            if node.args[1].value not in args[0].labels:
                raise ValueError(f"at: no period {node.args[1].value!r} among {args[0].labels}")
            return args[0].values[args[0].labels.index(node.args[1].value)]
        if name == "fill":  # Fills the periods the output has, including ones the row lacks.
            if len(args) != 2 or isinstance(args[1], (list, _Row)):
                raise ValueError("fill takes a list or value and a single replacement for null")
            row = _Row(target, args[0].at(target)) if isinstance(args[0], _Row) and target is not None else args[0]
            return _map(lambda a: args[1] if a is None else a, [row], target, keep_null=False)
        if name == "abs":
            return _map(abs, args, target)
        function = {"min": min, "max": max, "sum": sum}[name]
        if len(args) == 1 and isinstance(args[0], (list, _Row)):  # Aggregate one row across its periods.
            items = [a for a in (args[0].values if isinstance(args[0], _Row) else args[0]) if a is not None]
            return function(items) if items else None
        return _map(lambda *a: function(a), args, target)
    raise ValueError(f"formula cannot contain {type(node).__name__}")


def _check_value(key, value):
    if isinstance(value, bool) or not isinstance(value, (int, float, list)) or (
            isinstance(value, list) and not all(v is None or (isinstance(v, (int, float)) and not isinstance(v, bool))
                                                for v in value)):
        raise ValueError(f"Variable {key} has no numeric value or list of values")
    return value


def _map(function, operands, target, *, keep_null=True):
    """Apply per period, broadcasting scalars. Labelled rows are read at the target's labels, or must
    share labels when there is none; unlabelled lists must have equal lengths. A None operand yields
    None unless keep_null is false."""
    rows = [o for o in operands if isinstance(o, _Row)]
    lists = [o for o in operands if isinstance(o, list)]
    if rows:
        labels = target
        if labels is None:
            labels = rows[0].labels
            if any(r.labels != labels for r in rows):
                raise ValueError("formula combines rows with different periods and no output periods to align to")
        if any(len(l) != len(labels) for l in lists):
            raise ValueError("formula combines lists with different numbers of periods")
        operands = [o.at(labels) if isinstance(o, _Row) else o for o in operands]
        return _Row(labels, _map(function, operands, None, keep_null=keep_null))
    if not lists:
        if keep_null and any(o is None for o in operands):
            return None
        return _finite(function(*operands))
    if len({len(l) for l in lists}) != 1:
        raise ValueError("formula combines lists with different numbers of periods")
    return [_map(function, [o[i] if isinstance(o, list) else o for o in operands], None, keep_null=keep_null)
            for i in range(len(lists[0]))]


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
    by_key = _by_key(variables)
    values = {key: v.get("value") for key, v in by_key.items()}
    periods = {key: v["periods"] for key, v in by_key.items() if isinstance(v.get("periods"), list)}
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
            if problems:
                checked.append(VariableCheck(key, stored, None, False, tuple(problems)))
                continue
            computed = evaluate(variable["formula"], values, periods=periods, target=periods.get(key), self_key=key)
        except (ValueError, ZeroDivisionError, TypeError) as error:
            checked.append(VariableCheck(key, stored, None, False, (str(error),)))
            continue
        ok = matches(stored, computed, tolerance=tolerance)
        checked.append(VariableCheck(key, stored, computed, ok,
                                     () if ok else ("stored value differs from the formula's result",)))
    return ModelCheck(model_id, sum(v.get("role") == "input" for v in by_key.values()), len(outputs),
                      tuple(checked), tuple(v["key"] for v in outputs if v.get("formula") is None))


def _by_key(variables):
    by_key = {}
    for variable in variables:
        key = variable.get("key")
        if not isinstance(key, str) or not key:
            raise ValueError(f"Variable {variable.get('id')} has no key attribute")
        if key in by_key:
            raise ValueError(f"two Variables in the model share the key {key!r}")
        by_key[key] = variable
    return by_key


def evaluate_model(variables: list[dict[str, object]], model_id: str,
                   overrides: dict[str, object] | None = None) -> ModelEvaluation:
    """Recompute the model's formulas with some Variables set to other values.

    Unlike `check_model`, results flow through: each formula reads the recomputed values of the
    Variables it names. An override replaces a Variable's value and must have the stored value's shape
    (a number, or a list of the same length); an overridden output is held there and its formula is
    skipped. Nothing is written. A formula that cannot be evaluated raises ValueError naming its key.
    """
    by_key = _by_key(variables)
    overrides = {} if overrides is None else overrides
    if not isinstance(overrides, dict):
        raise ValueError("overrides must map Variable keys to values")
    for key, value in overrides.items():
        if key not in by_key:
            raise ValueError(f"override names a Variable not in the model: {key}")
        stored = by_key[key].get("value")
        _check_value(key, value)
        if stored is not None and (isinstance(value, list) != isinstance(stored, list) or (
                isinstance(value, list) and len(value) != len(stored))):
            raise ValueError(f"override for {key} must have the stored value's shape")
    periods = {key: v["periods"] for key, v in by_key.items() if isinstance(v.get("periods"), list)}
    formulas = {}
    for key, variable in by_key.items():
        if variable.get("formula") is not None:
            try:
                formulas[key] = parse(variable["formula"])[1] - {key}
            except ValueError as error:
                raise ValueError(f"{key}: {error}") from None

    def run(held):
        values = {key: v.get("value") for key, v in by_key.items()} | held
        order, state = [], {}

        def visit(key, path):
            if state.get(key) == "done" or key not in formulas or key in held:
                return
            if state.get(key) == "open":
                raise ValueError(f"formulas depend on each other in a cycle: {', '.join(path[path.index(key):])}")
            state[key] = "open"
            for name in sorted(formulas[key]):
                visit(name, path + [key])
            state[key] = "done"
            order.append(key)

        for key in formulas:
            visit(key, [])
        for key in order:
            names = formulas[key]
            try:
                values[key] = evaluate(by_key[key]["formula"], {n: values[n] for n in names if n in values},
                                       periods=periods, target=periods.get(key), self_key=key)
            except (ValueError, ZeroDivisionError, TypeError) as error:
                raise ValueError(f"{key}: {error}") from None
        return values, order

    baseline, order = run({})
    values, _ = run(dict(overrides))
    keys = [key for key in by_key if key not in order] + order
    return ModelEvaluation(
        model_id, dict(overrides),
        tuple(VariableValue(key, by_key[key].get("role"), by_key[key].get("value"), values[key],
                            not matches(baseline[key], values[key], tolerance=1e-12)) for key in keys),
        tuple(key for key, v in by_key.items() if v.get("role") == "output" and v.get("formula") is None))
