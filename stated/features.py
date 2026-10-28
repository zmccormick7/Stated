"""Feature definitions: the instructions an agent follows to produce one feature's value for a subject as of
a date, and the type of that value.

A definition is a procedure, not knowledge about the world, so it lives in its own table rather than among
the graph's records: every cutoff reads it alike, and it never appears in search or recall. A value is a
Variable whose `feature` attribute names the definition it was made under; Stated keeps the value within
the definition's type and nothing more. A definition can be edited until a value names it; after that, new
instructions are a new definition that `replaces` it, so each value stays tied to the instructions that
produced it, and `written_at` records when those instructions were fixed.

A definition may carry a schedule: the dates a value is due for each subject, every day, weekday, week, month
or quarter from a start date. A date is done for a subject once a value of the definition about it carries
that date, or once a check confirms that the latest value still held then; `pending` lists the rest.
"""

from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Iterable
import json
from uuid import uuid4

TYPES = ("number", "count", "boolean", "category")
EVERY = ("day", "weekday", "week", "month", "quarter")


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
    every: str | None  # The schedule: day, weekday, week, month or quarter, or None.
    start: str | None  # The schedule's first date.


@dataclass(frozen=True)
class Task:
    subject_id: str  # The Entity a value is due about.
    subject: str
    date: str


@dataclass(frozen=True)
class PendingWork:
    feature_id: str
    key: str
    start: str
    end: str
    total: int  # Scheduled dates still without a value or a check, over all the subjects.
    tasks: tuple[Task, ...]  # Up to the limit: subject by subject, each in date order.


@dataclass(frozen=True)
class FeatureTable:
    columns: tuple[str, ...]  # subject_id, subject, date, then KEY, KEY_as_of, KEY_checked for each feature.
    rows: tuple[tuple, ...]


@dataclass(frozen=True)
class Derived:
    key: str
    feature_id: str
    previous: object  # The latest value before, or None.
    value: object  # The computed value; None when it could not be computed.
    id: str | None  # The new value's ID, when one was written.
    problem: str | None  # Why nothing was written, when the value could not be computed or stored.


@dataclass(frozen=True)
class Coverage:
    definition: FeatureDefinition
    subjects: int  # Entities with a value of the definition.
    latest: str | None  # The date of its latest value, or None without one.


@dataclass(frozen=True)
class SubjectState:
    subject_id: str
    subject: str
    value_id: str  # The latest value of the definition about the subject.
    value: object
    as_of: str  # That value's date: when the feature last changed.
    checked: str  # The latest date it was read, by a new value or a check.
    values: int  # Values of the definition about the subject.


@dataclass(frozen=True)
class FeatureSubjects:
    definition: FeatureDefinition
    earlier: tuple[FeatureDefinition, ...]  # The definitions of the key it replaced, latest first.
    subjects: tuple[SubjectState, ...]
    in_sample: int  # Values dated on or before the day the instructions were written.
    due: int | None  # Scheduled dates still due for these subjects; None without a schedule or subjects.


@dataclass(frozen=True)
class SeriesPoint:
    id: str
    as_of: str
    value: object
    author_id: str
    in_sample: bool  # Dated on or before the day the instructions were written, so they may carry hindsight.


@dataclass(frozen=True)
class FeatureSeries:
    definition: FeatureDefinition
    subject_id: str
    subject: str
    points: tuple[SeriesPoint, ...]  # Every value about the subject in date order, later versions of a date last.
    checks: tuple[str, ...]  # The dates a check found the latest value still held.


@dataclass(frozen=True)
class FeatureCheck:
    value_id: str  # The latest value, found still to hold.
    feature_id: str
    subject_id: str
    as_of: str
    author_id: str
    written_at: str


_SELECT = """
    SELECT f.id, f.key, f.instructions, f.type, f.units, f.minimum, f.maximum, f.categories, f.formula,
           f.replaces, (SELECT n.id FROM features n WHERE n.replaces = f.id) AS replaced_by, f.author_id, f.written_at,
           (SELECT count(*) FROM records r WHERE r.kind = 'Variable'
              AND json_extract(r.attributes, '$.feature') = f.id) AS value_count, f.every, f.starts
    FROM features f
"""


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _decode(row) -> FeatureDefinition:
    categories = None if row["categories"] is None else tuple(json.loads(row["categories"]))
    return FeatureDefinition(row["id"], row["key"], row["instructions"], row["type"], row["units"], row["minimum"],
                             row["maximum"], categories, row["formula"], row["replaces"], row["replaced_by"],
                             row["author_id"], row["written_at"], row["value_count"], row["every"], row["starts"])


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


def schedule(store, feature: str, *, every: str | None, start: str | None = None) -> FeatureDefinition:
    """Set the dates a value of the feature is due for each subject, or clear them with `every` None. A
    schedule is not part of the instructions, so it can change after values exist."""
    _writable(store)
    definition = read(store, feature)
    if every is None and start is not None:
        raise ValueError("clearing a schedule takes no start")
    if every is not None:
        if every not in EVERY:
            raise ValueError(f"every is one of {', '.join(EVERY)}")
        _day(start, "start")
        if definition.formula is not None:
            raise ValueError("a derived feature has no schedule: it is computed whenever its inputs change")
    with store._db:
        store._db.execute("BEGIN IMMEDIATE")
        store._db.execute("UPDATE features SET every = ?, starts = ? WHERE id = ?", (every, start, definition.id))
    return read(store, definition.id)


def scheduled_dates(every: str, start: str, first: str, last: str) -> list[str]:
    """The schedule's dates from `first` to `last`, inclusive. A monthly or quarterly schedule keeps the start's
    day of the month, or the month's last day when it is shorter."""
    begin, low, high = date.fromisoformat(start), date.fromisoformat(first), date.fromisoformat(last)
    dates = []
    if every in ("day", "weekday", "week"):
        step = 7 if every == "week" else 1
        day = begin + timedelta(days=max(0, -(-(low - begin).days // step)) * step)
        while day <= high:
            if every != "weekday" or day.weekday() < 5:
                dates.append(day.isoformat())
            day += timedelta(days=step)
        return dates
    months = 1 if every == "month" else 3
    for n in range(0, 12 * (high.year - begin.year + 1) + 1, months):
        year, month = begin.year + (begin.month - 1 + n) // 12, (begin.month - 1 + n) % 12 + 1
        day = date(year, month, min(begin.day, monthrange(year, month)[1]))
        if day > high:
            break
        if day >= low:
            dates.append(day.isoformat())
    return dates


def pending(store, feature: str, subject_ids: Iterable[str], *, start: str | None = None, end: str | None = None,
            limit: int = 1000) -> PendingWork:
    """The scheduled dates from `start` (default: the schedule's start) to `end` (default: the store's cutoff,
    or today) on which a subject has neither a value of the feature's definition nor a check."""
    definition = read(store, feature)
    if definition.every is None:
        raise ValueError(f"feature {definition.key} has no schedule; set one with schedule-feature")
    if isinstance(subject_ids, (str, bytes)) or not isinstance(subject_ids, Iterable):
        raise ValueError("subject_ids must be a list of Entity IDs")
    subjects = [store.read(store.resolve_id(s)) for s in dict.fromkeys(subject_ids)]
    if not subjects or any(s.kind != "Entity" for s in subjects):
        raise ValueError("subject_ids must name one or more Entities")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100000:
        raise ValueError("limit must be an integer from 1 to 100,000")
    end = _day(end or store.as_of or datetime.now(UTC).date().isoformat(), "end")
    start = _day(start or definition.start, "start")
    if store.as_of is not None and end > store.as_of:
        raise ValueError(f"end is after the cutoff {store.as_of}")
    dates = scheduled_dates(definition.every, definition.start, start, end)
    total, tasks = 0, []
    for subject in subjects:
        done = {row[0] for row in store._db.execute(
            f"""SELECT v.as_of FROM records v JOIN links a ON a.from_key = v.record_key
                WHERE v.kind = 'Variable' AND json_extract(v.attributes, '$.feature') = ? AND a.relation = 'about'
                  AND a.to_key = ? AND {store._seen('v')} AND {store._seen('a', link=True)}
                UNION SELECT c.as_of FROM feature_checks c JOIN records v ON v.record_key = c.value_key
                JOIN links a ON a.from_key = v.record_key
                WHERE v.kind = 'Variable' AND json_extract(v.attributes, '$.feature') = ? AND a.relation = 'about'
                  AND a.to_key = ? AND c.as_of <= ?""",
            (definition.id, store._record_key(subject.id), definition.id, store._record_key(subject.id), end))}
        missing = [day for day in dates if day not in done]
        total += len(missing)
        tasks.extend(Task(subject.id, subject.text, day) for day in missing[:limit - len(tasks)])
    return PendingWork(definition.id, definition.key, start, end, total, tuple(tasks))


def confirm(store, value_id: str, *, author_id: str) -> FeatureCheck:
    """Record that a feature value, the latest of its definition about its subject, still holds on the store's
    date: the scheduled date is done without writing the same value again."""
    if not isinstance(author_id, str) or not author_id.strip():
        raise ValueError("author_id must be a nonblank string")
    value = store.read(store.resolve_id(value_id))
    feature_id = value.attributes.get("feature") if value.kind == "Variable" else None
    if not isinstance(feature_id, str):
        raise ValueError(f"{value.id} is not a feature value")
    subjects = [row[0] for row in store._db.execute(
        f"""SELECT e.record_key FROM links a JOIN records e ON e.record_key = a.to_key
            WHERE a.from_key = ? AND a.relation = 'about' AND e.kind = 'Entity' AND {store._seen('a', link=True)}""",
        (store._record_key(value.id),))]
    if len(subjects) != 1:
        raise ValueError(f"{value.id} must be about exactly one Entity to be confirmed for it")
    day = store._stamp()
    if value.as_of >= day:
        raise ValueError(f"{value.id} is dated {value.as_of}, so it already stands for that date")
    latest = store._db.execute(
        f"""SELECT v.id FROM records v JOIN links a ON a.from_key = v.record_key
            WHERE v.kind = 'Variable' AND json_extract(v.attributes, '$.feature') = ? AND a.relation = 'about'
              AND a.to_key = ? AND {store._seen('v')} ORDER BY v.as_of DESC, v.record_key DESC LIMIT 1""",
        (feature_id, subjects[0])).fetchone()[0]
    if latest != value.id:
        raise ValueError(f"{latest} is the latest value of this feature about its subject; confirm that one")
    written_at = datetime.now(UTC).isoformat()
    with store._db:
        store._db.execute("BEGIN IMMEDIATE")
        if store._db.execute("SELECT 1 FROM feature_checks WHERE value_key = ? AND as_of = ?",
                             (store._record_key(value.id), day)).fetchone():
            raise ValueError(f"{value.id} is already confirmed as of {day}")
        store._db.execute("INSERT INTO feature_checks (value_key, as_of, author_id, written_at) VALUES (?, ?, ?, ?)",
                          (store._record_key(value.id), day, author_id, written_at))
    subject_id = store._db.execute("SELECT id FROM records WHERE record_key = ?", (subjects[0],)).fetchone()[0]
    return FeatureCheck(value.id, feature_id, subject_id, day, author_id, written_at)


def derive(store, subject_id: str, *, author_id: str) -> tuple[Derived, ...]:
    """Compute every current derived feature for one subject as of the store's date, from the latest value
    about it of each feature its formula names, a derived feature after those it reads. Where the result
    differs from the latest value, or there is none, write a new value dated the store's date, linked
    `depends_on` the inputs and `updates` the previous value. Returns what was written or could not be."""
    from .formulas import evaluate, exact, parse
    if not isinstance(author_id, str) or not author_id.strip():
        raise ValueError("author_id must be a nonblank string")
    subject = store.read(store.resolve_id(subject_id))
    if subject.kind != "Entity":
        raise ValueError(f"{subject.id} is a {subject.kind}, not an Entity")
    current = {d.key: d for d in list_definitions(store)}
    ordered, placed = [], set()

    def place(definition, path=()):
        if definition.key in placed:
            return
        if definition.key in path:
            raise ValueError(f"derived features read each other in a loop: {' -> '.join(path + (definition.key,))}")
        for name in sorted(parse(definition.formula)[1]):
            if name in current and current[name].formula is not None:
                place(current[name], path + (definition.key,))
        placed.add(definition.key)
        ordered.append(definition)

    for definition in sorted(current.values(), key=lambda d: d.key):
        if definition.formula is not None:
            place(definition)
    results = []
    for definition in ordered:
        previous = _latest(store, definition.id, subject.id)
        prior = None if previous is None else previous.attributes.get("value")

        def skip(problem):
            results.append(Derived(definition.key, definition.id, prior, None, None, problem))

        inputs = {}
        for name in sorted(parse(definition.formula)[1]):
            value = _latest(store, current[name].id, subject.id) if name in current else None
            if value is None:
                break
            inputs[name] = value
        else:
            name = None
        if name is not None:
            skip(f"no value of {name} about this subject yet")
            continue
        if any(isinstance(v.attributes.get("value"), list) for v in inputs.values()):
            skip("inputs over several periods are not derived yet")
            continue
        periods = {v.attributes.get("period") for v in inputs.values()} - {None}
        if len(periods) > 1:
            skip(f"its inputs are for different periods: {', '.join(sorted(map(str, periods)))}")
            continue
        value = evaluate(definition.formula, {n: v.attributes.get("value") for n, v in inputs.items()})
        if previous is not None and (exact(prior, value) or (isinstance(value, bool) and prior is value)):
            continue
        attributes = {"key": definition.key, "feature": definition.id, "formula": definition.formula, "value": value}
        attributes |= {"period": periods.pop()} if periods else {}
        attributes |= {"units": definition.units} if definition.units else {}
        read_from = ", ".join(f"{n} = {v.attributes.get('value')!r}" for n, v in inputs.items())
        links = [{"from_id": "v", "to_id": subject.id, "relation": "about"}] + [
            {"from_id": "v", "to_id": v.id, "relation": "depends_on"} for v in inputs.values()] + (
            [{"from_id": "v", "to_id": previous.id, "relation": "updates"}] if previous is not None else [])
        try:
            made = store.contribute(author_id=author_id, records=[{
                "ref": "v", "kind": "Variable", "attributes": attributes,
                "text": f"{definition.key} of {subject.text}, computed by its formula from {read_from}."}],
                links=links).records["v"]
        except ValueError as error:
            skip(str(error))
            continue
        results.append(Derived(definition.key, definition.id, prior, value, made.id, None))
    return tuple(results)


def selected(store, features) -> dict[str, FeatureDefinition]:
    """Definitions by ID: each named by key (its current definition) or ID, or every current one; one per key."""
    if features is None:
        chosen = list_definitions(store)
    else:
        if isinstance(features, (str, bytes)) or not isinstance(features, Iterable):
            raise ValueError("features must be a list of keys or definition IDs, or None")
        features = tuple(features)
        if not features or any(not isinstance(f, str) or not f for f in features):
            raise ValueError("features must be a nonempty list of keys or definition IDs")
        chosen = [read(store, f) for f in dict.fromkeys(features)]
    keys = [d.key for d in chosen]
    if len(set(keys)) != len(keys):
        raise ValueError("name one definition per key")
    return {d.id: d for d in chosen}


def table(store, features, subject_ids, *, start: str, end: str | None, every: str) -> FeatureTable:
    """One row per subject and date, every day or weekday from `start` to `end` (default: the cutoff, or
    today). For each feature: the latest value dated on or before the date, `KEY_as_of`, the date that value
    is dated (when the feature last changed), and `KEY_checked`, the latest date it was read, by a new value
    or a check. A value dated a day rests on what was published by the end of that day. Subjects default to
    every Entity with a value of the features."""
    definitions = selected(store, features)
    if every not in ("day", "weekday"):
        raise ValueError("every is day or weekday")
    end = _day(end or store.as_of or datetime.now(UTC).date().isoformat(), "end")
    start = _day(start, "start")
    if start > end:
        raise ValueError("start must not be after end")
    if store.as_of is not None and end > store.as_of:
        raise ValueError(f"end is after the cutoff {store.as_of}")
    ids = f"json_extract(v.attributes, '$.feature') IN ({','.join('?' * len(definitions))})"
    if subject_ids is None:
        subjects = [(row[0], row[1]) for row in store._db.execute(
            f"""SELECT DISTINCT e.id, e.text FROM records v JOIN links a ON a.from_key = v.record_key
                JOIN records e ON e.record_key = a.to_key WHERE v.kind = 'Variable' AND {ids}
                AND a.relation = 'about' AND e.kind = 'Entity' AND {store._seen('v')} ORDER BY e.text, e.id""",
            tuple(definitions))]
    else:
        if isinstance(subject_ids, (str, bytes)) or not isinstance(subject_ids, Iterable):
            raise ValueError("subject_ids must be a list of Entity IDs")
        records = [store.read(store.resolve_id(s)) for s in dict.fromkeys(subject_ids)]
        if not records or any(r.kind != "Entity" for r in records):
            raise ValueError("subject_ids must name one or more Entities")
        subjects = [(r.id, r.text) for r in records]
    dates = scheduled_dates(every, start, start, end)
    order = sorted(definitions.values(), key=lambda d: d.key)
    rows = []
    for subject_id, subject in subjects:
        series = []
        for definition in order:
            values = [(row[0], json.loads(row[1]).get("value")) for row in store._db.execute(
                f"""SELECT v.as_of, v.attributes FROM records v JOIN links a ON a.from_key = v.record_key
                    WHERE v.kind = 'Variable' AND json_extract(v.attributes, '$.feature') = ? AND a.relation = 'about'
                      AND a.to_key = ? AND v.as_of <= ? AND {store._seen('v')} ORDER BY v.as_of, v.record_key""",
                (definition.id, store._record_key(subject_id), end))]
            checks = sorted({row[0] for row in store._db.execute(
                """SELECT c.as_of FROM feature_checks c JOIN records v ON v.record_key = c.value_key
                   JOIN links a ON a.from_key = v.record_key WHERE json_extract(v.attributes, '$.feature') = ?
                   AND a.relation = 'about' AND a.to_key = ? AND c.as_of <= ?""",
                (definition.id, store._record_key(subject_id), end))})
            series.append((values, checks))
        for day in dates:
            row = [subject_id, subject, day]
            for values, checks in series:
                known = [v for v in values if v[0] <= day]  # Later versions of a date follow earlier ones.
                if not known:
                    row += [None, None, None]
                    continue
                row += [known[-1][1], known[-1][0], max([known[-1][0]] + [c for c in checks if c <= day])]
            rows.append(tuple(row))
    columns = ("subject_id", "subject", "date") + tuple(
        name for d in order for name in (d.key, f"{d.key}_as_of", f"{d.key}_checked"))
    return FeatureTable(columns, tuple(rows))


def coverage(store) -> tuple[Coverage, ...]:
    """Each current definition with the number of subjects that have a value of it and its latest value's date."""
    found = []
    for definition in list_definitions(store):
        subjects, latest = store._db.execute(
            f"""SELECT count(DISTINCT a.to_key), max(v.as_of) FROM records v JOIN links a ON a.from_key = v.record_key
                JOIN records e ON e.record_key = a.to_key WHERE v.kind = 'Variable'
                AND json_extract(v.attributes, '$.feature') = ? AND a.relation = 'about' AND e.kind = 'Entity'
                AND {store._seen('v')} AND {store._seen('a', link=True)}""", (definition.id,)).fetchone()
        found.append(Coverage(definition, subjects, latest))
    return tuple(found)


def subjects(store, feature: str) -> FeatureSubjects:
    """A definition, named by key (its current one) or ID, with the latest value of every subject that has one,
    when it last changed and was last read, how many values were dated on or before the instructions were
    written, and how many scheduled dates are still due for those subjects."""
    definition = read(store, feature)
    earlier, previous = [], definition.replaces
    while previous is not None:
        earlier.append(read(store, previous))
        previous = earlier[-1].replaces
    checked = dict(store._db.execute(
        """SELECT e.id, max(c.as_of) FROM feature_checks c JOIN records v ON v.record_key = c.value_key
           JOIN links a ON a.from_key = v.record_key JOIN records e ON e.record_key = a.to_key
           WHERE json_extract(v.attributes, '$.feature') = ? AND a.relation = 'about' AND e.kind = 'Entity'
             AND (? IS NULL OR c.as_of <= ?) GROUP BY e.id""", (definition.id, store.as_of, store.as_of)))
    rows, in_sample = {}, 0
    for subject_id, subject, value_id, as_of, attributes in _values(store, definition.id):
        count = rows[subject_id].values + 1 if subject_id in rows else 1
        rows[subject_id] = SubjectState(subject_id, subject, value_id, json.loads(attributes).get("value"), as_of,
                                        max(as_of, checked.get(subject_id) or as_of), count)
        in_sample += as_of <= definition.written_at[:10]
    due = None
    if definition.every is not None and rows:
        due = pending(store, definition.id, list(rows), limit=1).total
    return FeatureSubjects(definition, tuple(earlier), tuple(rows.values()), in_sample, due)


def series(store, feature: str, subject_id: str) -> FeatureSeries:
    """Every value of a definition about one subject, in date order, each marked when it is dated on or before
    the day the instructions were written, and the dates checks found the latest value still held."""
    definition = read(store, feature)
    subject = store.read(store.resolve_id(subject_id))
    if subject.kind != "Entity":
        raise ValueError(f"{subject.id} is a {subject.kind}, not an Entity")
    points = tuple(SeriesPoint(value_id, as_of, json.loads(attributes).get("value"), author_id,
                               as_of <= definition.written_at[:10])
                   for value_id, as_of, attributes, author_id in store._db.execute(
        f"""SELECT v.id, v.as_of, v.attributes, v.author_id FROM records v JOIN links a ON a.from_key = v.record_key
            WHERE v.kind = 'Variable' AND json_extract(v.attributes, '$.feature') = ? AND a.relation = 'about'
              AND a.to_key = ? AND {store._seen('v')} AND {store._seen('a', link=True)}
            ORDER BY v.as_of, v.record_key""", (definition.id, store._record_key(subject.id))))
    checks = tuple(row[0] for row in store._db.execute(
        """SELECT DISTINCT c.as_of FROM feature_checks c JOIN records v ON v.record_key = c.value_key
           JOIN links a ON a.from_key = v.record_key WHERE json_extract(v.attributes, '$.feature') = ?
           AND a.relation = 'about' AND a.to_key = ? AND (? IS NULL OR c.as_of <= ?) ORDER BY c.as_of""",
        (definition.id, store._record_key(subject.id), store.as_of, store.as_of)))
    return FeatureSeries(definition, subject.id, subject.text, points, checks)


def _values(store, feature_id: str):
    """(subject ID, subject, value ID, date, attributes) for every value of a definition about an Entity,
    subject by subject in date order, later versions of a date last."""
    return store._db.execute(
        f"""SELECT e.id, e.text, v.id, v.as_of, v.attributes FROM records v JOIN links a ON a.from_key = v.record_key
            JOIN records e ON e.record_key = a.to_key WHERE v.kind = 'Variable'
            AND json_extract(v.attributes, '$.feature') = ? AND a.relation = 'about' AND e.kind = 'Entity'
            AND {store._seen('v')} AND {store._seen('a', link=True)} ORDER BY e.text, e.id, v.as_of, v.record_key""",
        (feature_id,))


def _latest(store, feature_id: str, subject_id: str):
    """The latest value of a definition about a subject that the store sees, or None."""
    row = store._db.execute(
        f"""SELECT v.id FROM records v JOIN links a ON a.from_key = v.record_key
            WHERE v.kind = 'Variable' AND json_extract(v.attributes, '$.feature') = ? AND a.relation = 'about'
              AND a.to_key = ? AND {store._seen('v')} AND {store._seen('a', link=True)}
            ORDER BY v.as_of DESC, v.record_key DESC LIMIT 1""", (feature_id, store._record_key(subject_id))).fetchone()
    return None if row is None else store.read(row[0])


def _day(value: object, name: str) -> str:
    try:
        if isinstance(value, str) and len(value) == 10:
            date.fromisoformat(value)
            return value
    except ValueError:
        pass
    raise ValueError(f"{name} must be a date, like 2023-02-24")


def _writable(store) -> None:
    if store.as_of is not None:
        raise ValueError("define features without a cutoff: their instructions are fixed in the present and "
                         "followed at every date")
