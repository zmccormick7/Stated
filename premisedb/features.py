"""Feature definitions: the instructions an agent follows to produce one feature's value for a subject as of
a date, and the type of that value.

A definition is a procedure, not knowledge about the world, so it lives in its own table rather than among
the graph's records: every cutoff reads it alike, and it never appears in search or recall. A value is a
Variable whose `feature` attribute names the definition it was made under; PremiseDB keeps the value within
the definition's type and nothing more. A definition can be edited until a value names it; after that, new
instructions are a new definition that `replaces` it, so each value stays tied to the instructions that
produced it, and `written_at` records when those instructions were fixed.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from uuid import uuid4

TYPES = ("number", "count", "boolean", "category")


@dataclass(frozen=True)
class FeatureDefinition:
    id: str
    key: str  # The name every value carries in its `key` attribute.
    instructions: str
    type: str  # number, count, boolean or category.
    units: str | None
    min: int | float | None
    max: int | float | None
    categories: tuple[str, ...] | None  # The values a category feature takes.
    formula: str | None  # For a derived feature, over other features' keys.
    replaces: str | None  # The definition of the same key this one took over from.
    replaced_by: str | None
    author_id: str
    written_at: str  # UTC time the instructions were last written.
    values: int  # Variables that name this definition.


_SELECT = """
    SELECT f.id, f.key, f.instructions, f.type, f.units, f.minimum, f.maximum, f.categories, f.formula,
           f.replaces, (SELECT n.id FROM features n WHERE n.replaces = f.id) AS replaced_by, f.author_id, f.written_at,
           (SELECT count(*) FROM records r WHERE r.kind = 'Variable'
              AND json_extract(r.attributes, '$.feature') = f.id) AS value_count
    FROM features f
"""


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _decode(row) -> FeatureDefinition:
    categories = None if row["categories"] is None else tuple(json.loads(row["categories"]))
    return FeatureDefinition(row["id"], row["key"], row["instructions"], row["type"], row["units"], row["minimum"],
                             row["maximum"], categories, row["formula"], row["replaces"], row["replaced_by"],
                             row["author_id"], row["written_at"], row["value_count"])


def _check(key, instructions, type, units, min, max, categories, formula, author_id) -> None:
    for name, value in (("key", key), ("instructions", instructions), ("author_id", author_id)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"a feature needs {name}, a nonblank string")
    if type not in TYPES:
        raise ValueError(f"a feature's type is one of {', '.join(TYPES)}")
    if units is not None and not isinstance(units, str):
        raise ValueError("units must be a string")
    if (type == "category") != (categories is not None):
        raise ValueError("a feature has categories exactly when its type is category")
    if categories is not None and (not isinstance(categories, (list, tuple)) or not categories
                                   or any(not isinstance(c, str) or not c.strip() for c in categories)
                                   or len(set(categories)) != len(categories)):
        raise ValueError("categories must be a list of distinct, nonblank strings")
    for name, bound in (("min", min), ("max", max)):
        if bound is not None and (type not in ("number", "count") or not _is_number(bound)):
            raise ValueError(f"{name} must be a number, on a feature of type number or count")
    if min is not None and max is not None and min > max:
        raise ValueError("min must not be above max")
    if formula is not None:
        from .formulas import parse
        parse(formula)


def check_value(attributes: dict[str, object], definition: FeatureDefinition) -> None:
    """A value carries its definition's key and formula, and is null, a value of its type, or a list of such
    values over periods."""
    if attributes.get("key") != definition.key:
        raise ValueError(f"a value of feature {definition.id} carries key {definition.key!r}, not {attributes.get('key')!r}")
    if attributes.get("formula") != definition.formula:
        raise ValueError(f"a value of feature {definition.id} carries its formula, {definition.formula!r}")

    def fits(value: object) -> bool:
        if value is None:
            return True
        if definition.type == "boolean":
            return isinstance(value, bool)
        if definition.type == "category":
            return isinstance(value, str) and value in definition.categories
        return (_is_number(value) and (definition.type == "number" or (value == int(value) and value >= 0))
                and (definition.min is None or value >= definition.min)
                and (definition.max is None or value <= definition.max))

    value = attributes.get("value")
    if not all(fits(v) for v in (value if isinstance(value, list) else [value])):
        expected = {"boolean": "true or false", "count": "a whole number, 0 or more", "number": "a number",
                    "category": f"one of {', '.join(map(repr, definition.categories or ()))}"}[definition.type]
        bounds = " ".join(part for part in (f"from {definition.min}" if definition.min is not None else "",
                                            f"to {definition.max}" if definition.max is not None else "") if part)
        raise ValueError(f"{value!r} is not a value of feature {definition.key}: it takes {expected}"
                         f"{' ' + bounds if bounds else ''}, or null")


def define(store, *, author_id: str, key: str, instructions: str, type: str, units: str | None = None,
           min: int | float | None = None, max: int | float | None = None, categories=None,
           formula: str | None = None, replaces: str | None = None) -> FeatureDefinition:
    _writable(store)
    _check(key, instructions, type, units, min, max, categories, formula, author_id)
    feature_id = str(uuid4())
    with store._db:
        store._db.execute("BEGIN IMMEDIATE")
        current = store._db.execute("""SELECT f.id FROM features f WHERE f.key = ?
                                       AND NOT EXISTS (SELECT 1 FROM features n WHERE n.replaces = f.id)""",
                                    (key,)).fetchone()
        if replaces is None and current is not None:
            raise ValueError(f"feature {key!r} is already defined as {current[0]}; reuse it, or define new "
                             "instructions with replaces naming it")
        if replaces is not None and (current is None or replaces != current[0]):
            raise ValueError(f"replaces must name the current definition of {key!r}"
                             + (f", {current[0]}" if current else "; there is none"))
        store._db.execute(
            """INSERT INTO features (id, key, instructions, type, units, minimum, maximum, categories, formula,
                                     replaces, author_id, written_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (feature_id, key, instructions, type, units, min, max,
             None if categories is None else json.dumps(list(categories)), formula, replaces, author_id,
             datetime.now(UTC).isoformat()))
    return read(store, feature_id)


def update(store, feature_id: str, *, author_id: str, instructions: str, type: str, units: str | None = None,
           min: int | float | None = None, max: int | float | None = None, categories=None,
           formula: str | None = None) -> FeatureDefinition:
    """Rewrite a definition no value names yet; its key and what it replaces stay."""
    _writable(store)
    current = read(store, feature_id)
    _check(current.key, instructions, type, units, min, max, categories, formula, author_id)
    with store._db:
        store._db.execute("BEGIN IMMEDIATE")
        if read(store, current.id).values:
            raise ValueError(f"values name feature {current.id}, so its definition is fixed; define new "
                             "instructions with replaces naming it")
        store._db.execute(
            """UPDATE features SET instructions = ?, type = ?, units = ?, minimum = ?, maximum = ?, categories = ?,
               formula = ?, author_id = ?, written_at = ? WHERE id = ?""",
            (instructions, type, units, min, max, None if categories is None else json.dumps(list(categories)),
             formula, author_id, datetime.now(UTC).isoformat(), current.id))
    return read(store, current.id)


def read(store, feature: str) -> FeatureDefinition:
    """A definition by ID, by a unique ID prefix of 8+ characters, or by key (its current definition)."""
    if not isinstance(feature, str) or not feature:
        raise KeyError(feature)
    rows = store._db.execute(_SELECT + " WHERE f.id = ?", (feature,)).fetchall() or \
        store._db.execute(_SELECT + """ WHERE f.key = ?
            AND NOT EXISTS (SELECT 1 FROM features n WHERE n.replaces = f.id)""", (feature,)).fetchall()
    if not rows and len(feature) >= 8:
        rows = store._db.execute(_SELECT + " WHERE f.id >= ? AND f.id < ? LIMIT 2",
                                 (feature, feature[:-1] + chr(ord(feature[-1]) + 1))).fetchall()
        if len(rows) > 1:
            raise ValueError(f"{feature} is a prefix of more than one feature ID")
    if not rows:
        raise KeyError(f"no feature {feature}")
    return _decode(rows[0])


def list_definitions(store, *, replaced: bool = False) -> tuple[FeatureDefinition, ...]:
    """Current definitions by key, or with `replaced` every definition, each key's in the order written."""
    where = "" if replaced else " WHERE NOT EXISTS (SELECT 1 FROM features n WHERE n.replaces = f.id)"
    return tuple(_decode(row) for row in store._db.execute(_SELECT + where + " ORDER BY f.key, f.feature_key"))


def _writable(store) -> None:
    if store.as_of is not None:
        raise ValueError("define features without a cutoff: their instructions are fixed in the present and "
                         "followed at every date")
