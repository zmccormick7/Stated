"""Current records and links; each store owns one thread-affine connection."""

from dataclasses import dataclass
from collections.abc import Iterable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
import json
import math
import re
import sqlite3
import unicodedata
from uuid import uuid4

from .chunking import split_ranges


_SCHEMA_VERSION = 15


@dataclass(frozen=True)
class Record:
    id: str
    kind: str
    text: str
    attributes: dict[str, object]
    author_id: str  # Last editor.
    created_by: str  # The record's creator; updates never change it.
    updated_at: str
    as_of: str  # The date the contents are as of: the cutoff the writer worked at, or the date of the write.
    layer: str  # The layer that owns the record; only the workspace's own layer accepts edits.


@dataclass(frozen=True)
class Link:
    id: str
    from_id: str
    to_id: str
    relation: str
    explanation: str
    author_id: str  # Last editor.
    created_by: str
    updated_at: str
    as_of: str
    layer: str


@dataclass(frozen=True)
class Layer:
    name: str
    version: str  # Empty for the workspace's own layer.
    writable: bool
    content_sha256: str | None = None  # Set for published layers, over their canonical contents.
    exported_at: str | None = None


@dataclass(frozen=True)
class Connections:
    links: tuple[Link, ...]
    has_more: bool
    records: dict[str, Record]  # The other end of each returned link, by ID.


@dataclass(frozen=True)
class Reached:
    record: Record
    depth: int  # 0 for a seed.
    path: tuple[Link, ...]  # Links from a seed to this record, in traversal order.


@dataclass(frozen=True)
class Expansion:
    reached: tuple[Reached, ...]  # Seeds first, then breadth-first; each record once.
    capped: tuple[str, ...]  # Records with more matching links than per_record_limit.
    has_more: bool  # The overall limit stopped the walk.


@dataclass(frozen=True)
class RecordPage:
    records: tuple[Record, ...]
    has_more: bool


@dataclass(frozen=True)
class LinkPage:
    links: tuple[Link, ...]
    has_more: bool


@dataclass(frozen=True)
class Citation:
    record: Record  # The citing record's current contents.
    source_id: str
    start_byte: int
    end_byte: int


@dataclass(frozen=True)
class CitationPage:
    citations: tuple[Citation, ...]
    has_more: bool


@dataclass(frozen=True)
class RelationUsage:
    relation: str
    from_kind: str
    to_kind: str
    links: int


@dataclass(frozen=True)
class Contribution:
    records: dict[str, Record]  # Keyed by the caller's ref.
    links: tuple[Link, ...]  # Explicit links only; citation links are not repeated here.


@dataclass(frozen=True)
class Merge:
    """The outcome of folding a duplicate record into the one kept."""
    keep_id: str
    duplicate_id: str
    moved: int  # Links moved from the duplicate to the kept record.
    dropped: int  # Links the duplicate had that the kept record already had.


@dataclass(frozen=True)
class FilledVariable:
    key: str
    stored: object  # The value before the fill.
    value: object  # The formula's result, now stored.
    id: str  # The Variable holding it: the output itself, or a new version of it when the output could not be edited.


@dataclass(frozen=True)
class ModelFill:
    model_id: str  # The Model, Entity or Variable the fill was asked for.
    written: tuple[FilledVariable, ...]  # Formulas whose stored value was not exactly their result.


@dataclass(frozen=True)
class StateRow:
    """One Entity's feature values on one date: the latest version of each feature stamped by then."""
    entity_id: str
    entity: str
    date: str
    values: dict[str, object]  # Feature key → value as of the date.
    periods: dict[str, list]  # Labels of the keys whose value is a row.
    stamps: dict[str, str]  # Feature key → the as_of of the version in use: when that feature last changed.


@dataclass(frozen=True)
class Passage:
    source_id: str
    ordinal: int
    start_byte: int
    end_byte: int


@dataclass(frozen=True)
class DocumentContext:
    source_id: str
    title: str
    summary: str
    author_id: str
    updated_at: str
    provenance: dict[str, object]

    @property
    def header(self) -> str:
        return f"{self.title}\n{self.summary}"


@dataclass(frozen=True)
class SourceSegment:
    source_id: str
    start_byte: int
    end_byte: int
    text: str
    document_context: DocumentContext | None = None


KINDS = ("Entity", "Source", "Finding", "Question", "Model", "Variable", "Feature")
# A new record named by its contribute ref inside text or an explanation, replaced by the record's ID.
_REF = re.compile(r"\{ref:([^{}]+)\}")
# Characters that extracted text often swaps for one another, used only to suggest a corrected quote.
_LOOKALIKES = ("-\u2010\u2011\u2012\u2013\u2014\u2212", "'\u2018\u2019\u02bc\u2032", '"\u201c\u201d\u2033')


def _check_limit(value: int, name: str = "limit") -> None:
    if type(value) is not int or not 1 <= value <= 1000:
        raise ValueError(f"{name} must be an integer from 1 to 1000")


def _refuse_refs(text: object) -> None:
    """Keep {ref:NAME} placeholders out of writes that have no refs to resolve them."""
    if isinstance(text, str) and _REF.search(text):
        raise ValueError("{ref:NAME} placeholders are resolved only in contribute; use the record ID")


def _fill_refs(text: object, ids: dict[str, str], where: str) -> object:
    """Replace each {ref:NAME} with the ID of the record contributed under that ref."""
    if not isinstance(text, str):
        return text  # The write itself reports the invalid type.
    def replace(match: re.Match) -> str:
        if match.group(1) not in ids:
            raise ValueError(f"{where}: {match.group(0)} names no record in this contribution")
        return ids[match.group(1)]
    return _REF.sub(replace, text)


def _date(value: str):
    """The date a YYYY-MM-DD string names, or None."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


FEATURE_TYPES = ("number", "count", "boolean", "category")
_DEFINING = ("key", "type", "units", "min", "max", "categories", "formula")  # Fixed once a value rests on them.


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_definition(attributes: dict[str, object]) -> None:
    """A Feature names the key its values carry and their type: a number, optionally within `min` and `max`;
    a count; yes or no; or one of a fixed list of `categories`. A derived Feature also carries its `formula`."""
    kind, categories = attributes.get("type"), attributes.get("categories")
    if not isinstance(attributes.get("key"), str) or not attributes["key"].strip():
        raise ValueError("a Feature needs attributes.key, the name its values carry")
    if kind not in FEATURE_TYPES:
        raise ValueError(f"a Feature needs attributes.type, one of {', '.join(FEATURE_TYPES)}")
    if (kind == "category") != (categories is not None):
        raise ValueError("a Feature has attributes.categories exactly when its type is category")
    if categories is not None and (not isinstance(categories, list) or not categories or len(set(map(str, categories)))
                                   != len(categories) or any(not isinstance(c, str) or not c.strip() for c in categories)):
        raise ValueError("attributes.categories must be a list of distinct, nonblank strings")
    for bound in ("min", "max"):
        if attributes.get(bound) is not None and (kind not in ("number", "count") or not _is_number(attributes[bound])):
            raise ValueError(f"attributes.{bound} must be a number, on a Feature of type number or count")
    if _is_number(attributes.get("min")) and _is_number(attributes.get("max")) and attributes["min"] > attributes["max"]:
        raise ValueError("attributes.min must not be above attributes.max")
    if attributes.get("formula") is not None and not isinstance(attributes["formula"], str):
        raise ValueError("attributes.formula must be a string")


def _check_value(attributes: dict[str, object], feature: "Record") -> None:
    """A value of a Feature carries its key and its formula, and is null, a value of its type, or a list of
    such values over periods."""
    definition = feature.attributes
    if attributes.get("key") != definition["key"]:
        raise ValueError(f"a value of Feature {feature.id} carries key {definition['key']!r}, not {attributes.get('key')!r}")
    if attributes.get("formula") != definition.get("formula"):
        raise ValueError(f"a value of Feature {feature.id} carries its formula, {definition.get('formula')!r}")
    kind, low, high = definition["type"], definition.get("min"), definition.get("max")

    def fits(value: object) -> bool:
        if value is None:
            return True
        if kind == "boolean":
            return isinstance(value, bool)
        if kind == "category":
            return isinstance(value, str) and value in definition["categories"]
        return (_is_number(value) and (kind == "number" or (value == int(value) and value >= 0))
                and (low is None or value >= low) and (high is None or value <= high))

    value = attributes.get("value")
    if not all(fits(v) for v in (value if isinstance(value, list) else [value])):
        expected = {"boolean": "true or false", "count": "a whole number, 0 or more", "number": "a number",
                    "category": f"one of {', '.join(map(repr, definition.get('categories') or ()))}"}[kind]
        bounds = " ".join(part for part in (f"from {low}" if low is not None else "",
                                            f"to {high}" if high is not None else "") if part)
        raise ValueError(f"{value!r} is not a value of Feature {definition['key']}: it takes {expected}"
                         f"{' ' + bounds if bounds else ''}, or null")


def _check_kind(kind: str | None) -> None:
    if kind is not None and kind not in KINDS:
        raise ValueError("kind must be a record kind or None")


_RECORD_SELECT = """
    SELECT r.id, r.kind, r.text, r.attributes, r.author_id, r.created_by, r.updated_at, r.as_of, y.name AS layer
    FROM records r JOIN layers y ON y.layer_key = r.layer_key
"""

_LINK_SELECT = """
    SELECT l.id, f.id AS from_id, t.id AS to_id, l.relation,
           l.explanation, l.author_id, l.created_by, l.updated_at, l.as_of, y.name AS layer
    FROM links l JOIN records f ON f.record_key = l.from_key
    JOIN records t ON t.record_key = l.to_key JOIN layers y ON y.layer_key = l.layer_key
"""

# New rows join the workspace's own layer, the only one that accepts writes.
_WRITABLE_LAYER = "(SELECT layer_key FROM layers WHERE writable = 1)"


class RecordStore:
    def __init__(self, path: str | Path, *, layer: str = "workspace", as_of: str | None = None):
        """Open a store, creating it with `layer` as the name of its own writable layer if new.

        With `as_of`, a date, every read sees only what a reader on that date could have seen (the
        record_visibility view in schema.sql says what that is), and every write is stamped with it.
        """
        if not isinstance(layer, str) or not layer.strip():
            raise ValueError("layer must be a nonblank string")
        if as_of is not None and (not isinstance(as_of, str) or len(as_of) != 10 or _date(as_of) is None):
            raise ValueError("as_of must be a date, like 2023-02-24, or None")
        self.as_of = as_of
        if sqlite3.sqlite_version_info < (3, 51, 3):
            raise RuntimeError("SQLite 3.51.3+ is required for the WAL-reset fix")
        self._db = sqlite3.connect(path, isolation_level=None, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        try:
            version = self._schema_version()
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = FULL")
            self._db.execute("PRAGMA foreign_keys = ON")
            if version == 0:
                self._initialize(layer)
        except BaseException:
            self._db.close()
            raise

    def _schema_version(self) -> int:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, _SCHEMA_VERSION):
            raise ValueError(
                f"Incompatible schema version {version}; expected {_SCHEMA_VERSION}. "
                "Migrations are not supported; create a new database."
            )
        return version

    def _initialize(self, layer: str) -> None:
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            if self._schema_version() == _SCHEMA_VERSION:
                return
            # executescript would commit before running; keep schema creation atomic.
            statement = ""
            for line in Path(__file__).with_name("schema.sql").read_text().splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    self._db.execute(statement)
                    statement = ""
            if statement.strip():
                raise ValueError("Incomplete statement in schema.sql")
            self._db.execute("INSERT INTO layers (name, version, writable) VALUES (?, '', 1)", (layer,))
            self._db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def close(self) -> None:
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def layers(self) -> tuple[Layer, ...]:
        """The workspace's own layer first, then attached layers in attachment order."""
        rows = self._db.execute(
            """SELECT name, version, writable, content_sha256, exported_at FROM layers
               ORDER BY writable DESC, layer_key""").fetchall()
        return tuple(Layer(row["name"], row["version"], bool(row["writable"]), row["content_sha256"],
                           row["exported_at"]) for row in rows)

    def export_layer(self, path: str | Path, *, version: str) -> "LayerExport":
        """Write the workspace's own layer to a new database file as a sealed, published layer."""
        from .layers import export_layer
        return export_layer(self, path, version=version)

    def check_model(self, model_id: str, *, tolerance: float = 1e-4) -> "ModelCheck":
        """Check that each formula reproduces its stored value. The ID names a Model (checked over the latest
        version of each of its Variables), a feature Variable (its formula against the versions it depends
        on), or an Entity (the latest version of each feature about it, computed from one another)."""
        from .formulas import check_model
        model_id, variables = self._variables_of(model_id, latest=False)
        return check_model(variables, model_id, tolerance=tolerance)

    def evaluate_model(self, model_id: str, overrides: dict[str, object] | None = None) -> "ModelEvaluation":
        """Recompute the formulas with some Variables set to other values; nothing is written. The ID names a
        Model, an Entity (its features) or a feature Variable (computed from the latest version of each
        input about its Entity)."""
        from .formulas import evaluate_model
        model_id, variables = self._variables_of(model_id, latest=True)
        return evaluate_model(variables, model_id, overrides)

    def fill_model(self, model_id: str, *, author_id: str) -> ModelFill:
        """Store each formula's value where the stored value is not exactly that, results flowing through
        dependent formulas as in evaluate_model. The ID names a Model, an Entity (every feature about it, each
        computed from the latest version of its inputs) or one feature Variable. A Variable with no value yet
        that this store may edit is filled in place, linked `depends_on` the input versions used; any other
        gets a new version: the same attributes with the new value, carrying its `in_model` or `about` link,
        linked `updates` the old version and `depends_on` the input versions used, so the old value stays
        true of its own date."""
        from .formulas import evaluate_model, exact, parse
        model_id, variables = self._variables_of(model_id, latest=True)
        by_key = {v["key"]: v for v in variables}
        written = []
        for item in evaluate_model(variables, model_id).variables:
            variable = by_key[item.key]
            if variable.get("formula") is None or exact(item.stored, item.value):
                continue
            current = self.read(variable["id"])
            inputs = [by_key[n]["id"] for n in sorted(parse(variable["formula"])[1] - {item.key}) if n in by_key]
            try:
                if item.stored is not None:
                    raise ValueError("a changed value is a new version")  # Resting on the current inputs.
                self._check_editable(current)
                record = self.patch(current.id, author_id=author_id, attributes={"value": item.value})
                with self._db:
                    self._db.execute("BEGIN IMMEDIATE")
                    self._link_each(record.id, inputs, "depends_on", author_id)
            except ValueError:
                kept = [{"from_id": "v", "to_id": link.to_id, "relation": link.relation, "explanation": link.explanation}
                        for link in self._links_of(current.id, "out", None, 1000)[0]
                        if link.relation not in ("updates", "supersedes", "depends_on")]
                record = self.contribute(author_id=author_id, records=[{
                    "ref": "v", "kind": "Variable", "text": current.text,
                    "attributes": {k: v for k, v in current.attributes.items() if k != "citations"} | {"value": item.value}}],
                    links=kept + [{"from_id": "v", "to_id": current.id, "relation": "updates",
                                   "explanation": "The same quantity as of a later date, recomputed from its formula."}]
                          + [{"from_id": "v", "to_id": i, "relation": "depends_on"} for i in inputs]).records["v"]
            by_key[item.key] = variable | {"id": record.id}
            written.append(FilledVariable(item.key, item.stored, item.value, record.id))
        return ModelFill(model_id, tuple(written))

    def _link_each(self, from_id: str, to_ids: Iterable[str], relation: str, author_id: str) -> None:
        """Add a link to each target that is not already linked that way, inside the caller's transaction."""
        for to_id in to_ids:
            values = self._link_values(from_id, to_id, relation, "", author_id)
            if self._db.execute("SELECT 1 FROM links WHERE from_key = ? AND to_key = ? AND relation = ?",
                                values[:3]).fetchone() is None:
                self._db.execute(
                    f"""INSERT INTO links (from_key, to_key, relation, explanation, author_id, updated_at,
                        as_of, id, created_by, layer_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, {_WRITABLE_LAYER})""",
                    (*values, self._stamp(), str(uuid4()), author_id))

    def state_rows(self, *, entity_id: str | None = None, features: Iterable[str] | None = None,
                   start: str | None = None, end: str | None = None, limit: int = 1000) -> tuple[StateRow, ...]:
        """Feature values over a date range, as change points. A feature value is a Variable `about` an
        Entity, named by its `key`. For each Entity with any, a row at `start` holding the latest version
        of each feature stamped by then, and a row at every later date up to `end` on which some feature
        changed. Between rows the state is unchanged, so a daily series is a forward fill. `end` defaults to
        the store's cutoff, or today, and `start` to `end`, so with neither there is one row per Entity: its
        current state. `features` keeps only those keys."""
        if entity_id is not None and not isinstance(entity_id, str):
            raise ValueError("entity_id must be a string or None")
        if features is not None:
            if isinstance(features, (str, bytes)) or not isinstance(features, Iterable):
                raise ValueError("features must be a list of keys or None")
            features = tuple(features)
            if not features or any(not isinstance(key, str) or not key for key in features):
                raise ValueError("features must be a nonempty list of keys")
        _check_limit(limit)
        today = self.as_of or datetime.now(UTC).date().isoformat()
        end = end or today
        start = start or end
        for name, value in (("start", start), ("end", end)):
            if not isinstance(value, str) or len(value) != 10 or _date(value) is None:
                raise ValueError(f"{name} must be a date, like 2023-02-24")
        if start > end:
            raise ValueError("start must not be after end")
        if self.as_of is not None and end > self.as_of:
            raise ValueError(f"end is after the cutoff {self.as_of}")
        entity_key = None if entity_id is None else self._record_key(entity_id)
        entities = self._db.execute(
            f"""SELECT DISTINCT e.record_key, e.id, e.text
                FROM links a JOIN records v ON v.record_key = a.from_key JOIN records e ON e.record_key = a.to_key
                WHERE a.relation = 'about' AND v.kind = 'Variable' AND e.kind = 'Entity'
                  AND (? IS NULL OR e.record_key = ?) AND {self._seen('a', link=True)}
                ORDER BY e.text, e.record_key""", (entity_key, entity_key)).fetchall()
        result = []
        for entity in entities:
            # Every version of every feature about the Entity stamped by the end, oldest first.
            versions = [(json.loads(row["attributes"]), row["as_of"]) for row in self._db.execute(
                f"""SELECT v.attributes, v.as_of FROM links a JOIN records v ON v.record_key = a.from_key
                    WHERE a.to_key = ? AND a.relation = 'about' AND v.kind = 'Variable'
                      AND v.as_of <= ? AND a.as_of <= ? AND {self._seen('a', link=True)}
                    ORDER BY v.as_of, v.record_key""", (entity["record_key"], end, end))]
            versions = [(a, stamp) for a, stamp in versions
                        if isinstance(a.get("key"), str) and (features is None or a["key"] in features)]
            dates = sorted({start} | {stamp for _, stamp in versions if start < stamp <= end})
            for date in dates:
                state = {}
                for attributes, stamp in versions:  # Later versions overwrite earlier ones.
                    if stamp <= date:
                        state[attributes["key"]] = (attributes, stamp)
                if not state:
                    continue  # Nothing was written by this date.
                if len(result) == limit:
                    return tuple(result)
                result.append(StateRow(
                    entity["id"], entity["text"], date,
                    {key: attributes.get("value") for key, (attributes, _) in state.items()},
                    {key: attributes["periods"] for key, (attributes, _) in state.items()
                     if isinstance(attributes.get("periods"), list)},
                    {key: stamp for key, (_, stamp) in state.items()}))
        return tuple(result)

    def _variables_of(self, record_id: str, *, latest: bool):
        """The Variables a model operation works over, in the formulas' format: each one's attributes with
        its `id`, its `stamp` and its `depends_on` keys within the set. For a Model, the latest version of
        each key among its Variables; for an Entity, the latest version of each feature about it; for a
        feature Variable, itself and its inputs, which are the latest version about its Entity of each key
        its formula names when `latest`, else the versions it depends on, taken as given values."""
        record = self.read(self.resolve_id(record_id))
        if record.kind in ("Model", "Entity"):
            return record.id, self._members(record, "in_model" if record.kind == "Model" else "about")
        if record.kind != "Variable":
            raise ValueError(f"{record_id} is a {record.kind}, not a Model, Entity or Variable")
        from .formulas import parse
        declared = [json.loads(row["attributes"]).get("key") for row in self._db.execute(
            f"""SELECT d.attributes FROM links l JOIN records d ON d.record_key = l.to_key
                WHERE l.from_key = ? AND l.relation = 'depends_on' AND d.kind = 'Variable'
                  AND {self._seen('l', link=True)}""", (self._record_key(record.id),))]
        feature = record.attributes | {"id": record.id, "stamp": record.as_of,
                                       "depends_on": [k for k in declared if isinstance(k, str)]}
        if latest and record.attributes.get("formula") is not None:
            names = parse(record.attributes["formula"])[1] - {record.attributes.get("key")}
            entity = next((l.to_id for l in self._links_of(record.id, "out", ("about",), 1)[0]), None)
            inputs = [v for v in self._members(self.read(entity), "about")
                      if v["key"] in names] if entity else []
        else:
            inputs = self._versions(f"""SELECT d.record_key, d.id, d.attributes, d.as_of FROM links l
                JOIN records d ON d.record_key = l.to_key WHERE l.from_key = ? AND l.relation = 'depends_on'
                AND d.kind = 'Variable' AND {self._seen('l', link=True)}""", (self._record_key(record.id),))
        return record.id, [feature] + [{k: v for k, v in i.items() if k != "formula"} | {"depends_on": []}
                                       for i in inputs if i.get("key") != feature.get("key")]

    def _members(self, holder: Record, relation: str):
        """The latest version of each key among the Variables linked by `relation` to the holder, with
        their `depends_on` keys within that set."""
        return self._versions(f"""SELECT v.record_key, v.id, v.attributes, v.as_of FROM links l
            JOIN records v ON v.record_key = l.from_key WHERE l.to_key = ? AND l.relation = ?
            AND v.kind = 'Variable' AND {self._seen('l', link=True)}""", (self._record_key(holder.id), relation))

    def _versions(self, members_sql: str, parameters: tuple):
        """Reduce a query for Variables (record_key, id, attributes, as_of) to the latest version per key,
        each in the formulas' format."""
        rows = self._db.execute(
            f"""WITH members AS (
                    SELECT *, row_number() OVER (PARTITION BY coalesce(json_extract(attributes, '$.key'), id)
                                                 ORDER BY as_of DESC, record_key DESC) AS recency
                    FROM ({members_sql}))
                SELECT v.id, v.attributes, v.as_of,
                       (SELECT json_group_array(json_extract(d.attributes, '$.key')) FROM links dl
                        JOIN records d ON d.record_key = dl.to_key
                        WHERE dl.from_key = v.record_key AND dl.relation = 'depends_on'
                          AND d.record_key IN (SELECT record_key FROM members)) AS depends_on
                FROM members v WHERE v.recency = 1 ORDER BY v.record_key""", parameters).fetchall()
        return [json.loads(row["attributes"]) | {"id": row["id"], "stamp": row["as_of"], "depends_on": [
            key for key in json.loads(row["depends_on"]) if key is not None]} for row in rows]

    def attach_layer(self, path: str | Path) -> "LayerAttachment":
        """Attach a published layer file read-only, or bring an attached layer to the file's version."""
        from .layers import attach_layer
        return attach_layer(self, path)

    def _seen(self, alias: str, link: bool = False) -> str:
        """A condition keeping the records (or links) `alias` names that a read at the cutoff sees."""
        if self.as_of is None:
            return "1"
        view, key = ("link_visibility", "link_key") if link else ("record_visibility", "record_key")
        return (f"EXISTS (SELECT 1 FROM {view} v WHERE v.{key} = {alias}.{key} "
                f"AND v.visible_from <= '{self.as_of}')")

    def _stamp(self) -> str:
        return self.as_of or datetime.now(UTC).date().isoformat()

    def _check_editable(self, item: "Record | Link") -> None:
        """A row written at a cutoff is edited only at that cutoff, and one written without a cutoff only
        without one, so what a reader at a date sees is not rewritten from another date. Entities are
        reference data, always seen, and exempt."""
        if getattr(item, "kind", None) == "Entity":
            return
        written_at = item.as_of if item.as_of != item.updated_at[:10] else None
        if written_at != self.as_of:
            describe = lambda cutoff: f"as of {cutoff}" if cutoff else "without a cutoff"
            raise ValueError(f"{item.id} was written {describe(written_at)} and cannot be edited "
                             f"{describe(self.as_of)}; add your own record and link it (updates or supersedes)")

    def _check_published(self, attributes: dict[str, object] | None) -> None:
        """At a cutoff, a Source must carry a publication date on or before it, or it would never be seen."""
        if self.as_of is None:
            return
        published = (attributes or {}).get("published")
        if not isinstance(published, str) or _date(published[:10]) is None or published[:10] > self.as_of:
            raise ValueError(f"a Source written as of {self.as_of} needs attributes.published, a date on or before "
                             f"the cutoff; it has {published!r}")

    def _missing(self, record_id: str, link: bool = False) -> KeyError:
        """KeyError for an ID or prefix a read found nothing for, saying so when the cutoff is the reason."""
        table = "links" if link else "records"
        if self.as_of is not None and isinstance(record_id, str) and len(record_id) >= 8 and self._db.execute(
                f"SELECT 1 FROM {table} WHERE id >= ? AND id < ?",
                (record_id, record_id[:-1] + chr(ord(record_id[-1]) + 1))).fetchone():
            return KeyError(f"{record_id} is not visible as of {self.as_of}")
        return KeyError(record_id)

    def read(self, record_id: str) -> Record:
        """Read current contents; a missing record raises KeyError."""
        row = self._db.execute(_RECORD_SELECT + f" WHERE r.id = ? AND {self._seen('r')}", (record_id,)).fetchone()
        if row is None:
            raise self._missing(record_id)
        return self._decode_record(row)

    def resolve_id(self, value: str, *, link: bool = False) -> str:
        """Return the full record ID, or link ID, for an exact ID or a unique prefix of 8+ characters."""
        table = "links" if link else "records"
        if not isinstance(value, str) or not value:
            raise ValueError("an ID must be a nonempty string")
        seen = self._seen("x", link)
        if self._db.execute(f"SELECT 1 FROM {table} x WHERE x.id = ? AND {seen}", (value,)).fetchone():
            return value
        if len(value) < 8:
            raise self._missing(value, link)
        # IDs sharing the prefix sort between it and the prefix with its last character incremented.
        rows = self._db.execute(f"SELECT id FROM {table} x WHERE x.id >= ? AND x.id < ? AND {seen} ORDER BY id LIMIT 2",
                                (value, value[:-1] + chr(ord(value[-1]) + 1))).fetchall()
        if not rows:
            raise self._missing(value, link)
        if len(rows) > 1:
            raise ValueError(f"ID prefix {value} matches more than one {'link' if link else 'record'}; "
                             "use more characters")
        return rows[0][0]

    def search_records(self, query: str, *, kind: str | None = None,
                       limit: int = 10) -> tuple[Record, ...]:
        """Match any plain-text query word; return current records in BM25 order."""
        expression = self._search_expression(query)
        _check_kind(kind)
        _check_limit(limit)
        if not expression:
            return ()
        rows = self._db.execute(
            _RECORD_SELECT + f"""JOIN records_fts ON records_fts.rowid = r.record_key
               WHERE records_fts MATCH ? AND (? IS NULL OR r.kind = ?) AND {self._seen('r')}
               ORDER BY bm25(records_fts), r.record_key LIMIT ?""",
            (expression, kind, kind, limit),
        ).fetchall()
        return tuple(self._decode_record(row) for row in rows)

    def search_documents(self, query: str, *, source_ids: Iterable[str] | None = None,
                         limit: int = 10) -> tuple[SourceSegment, ...]:
        """Rank passage headers/bodies; return exact source text with separate context."""
        expression = self._search_expression(query)
        _check_limit(limit)
        source_keys = None
        if source_ids is not None:
            if isinstance(source_ids, (str, bytes)) or not isinstance(source_ids, Iterable):
                raise ValueError("source_ids must be an iterable of strings or None")
            ids = tuple(source_ids)
            if any(not isinstance(source_id, str) for source_id in ids):
                raise ValueError("source_ids must contain only strings")
            source_keys = json.dumps([self._source_key(source_id) for source_id in dict.fromkeys(ids)])
        if not expression or source_keys == "[]":
            return ()
        rows = self._db.execute(
            f"""SELECT r.id AS source_id, p.start_byte, p.end_byte,
                      c.title, c.summary, c.author_id, c.updated_at, c.provenance
               FROM passages_fts JOIN passages p ON p.passage_key = passages_fts.rowid
               JOIN records r ON r.record_key = p.source_key
               LEFT JOIN document_context c ON c.source_key = p.source_key
               WHERE passages_fts MATCH ? AND {self._seen('r')}
                 AND (? IS NULL OR p.source_key IN (SELECT value FROM json_each(?)))
               ORDER BY bm25(passages_fts, 0.5, 1.0), p.source_key, p.ordinal LIMIT ?""",
            (expression, source_keys, source_keys, limit),
        ).fetchall()
        # Context was captured with the hits; fixed source ranges survive rechunking.
        return tuple(SourceSegment(
            row["source_id"], row["start_byte"], row["end_byte"],
            self.read_source_text(row["source_id"], start_byte=row["start_byte"],
                                  end_byte=row["end_byte"]),
            self._decode_document_context(row["source_id"], row),
        ) for row in rows)

    def _rse_intervening_passages(self, candidates: tuple[SourceSegment, ...],
                                max_segment_passages: int) -> tuple[SourceSegment, ...]:
        """Read unreturned passages between hits for RSE penalties and token accounting."""
        if not candidates:
            return ()
        anchors = [(s.source_id, s.start_byte, s.end_byte) for s in candidates]
        rows = self._db.execute(
            """WITH anchors AS (
                   SELECT CAST(j.key AS INTEGER) AS hit, p.source_key, p.ordinal,
                          lead(p.ordinal) OVER (
                              PARTITION BY p.source_key ORDER BY p.ordinal) AS next_ordinal
                   FROM json_each(?) j
                   LEFT JOIN records r ON r.id = json_extract(j.value, '$[0]')
                   LEFT JOIN passages p ON p.source_key = r.record_key
                     AND p.start_byte = json_extract(j.value, '$[1]')
                     AND p.end_byte = json_extract(j.value, '$[2]')
               )
               SELECT a.hit, a.ordinal, p.start_byte, p.end_byte
               FROM anchors a LEFT JOIN passages p ON p.source_key = a.source_key
                 AND p.ordinal > a.ordinal AND p.ordinal < a.next_ordinal
                 AND a.next_ordinal - a.ordinal < ?
               ORDER BY a.hit, p.ordinal""",
            (json.dumps(anchors), max_segment_passages),
        ).fetchall()
        if any(row["ordinal"] is None for row in rows):
            raise ValueError("A hit is no longer a current passage; repeat document search")
        # Larger gaps cannot fit the passage limit. Never read outside the hits.
        # Capture locators before body reads; retain the search-time context.
        intervening = []
        for row in rows:
            if row["start_byte"] is not None:
                hit = candidates[row["hit"]]
                intervening.append(SourceSegment(
                    hit.source_id, row["start_byte"], row["end_byte"],
                    self.read_source_text(hit.source_id, start_byte=row["start_byte"], end_byte=row["end_byte"]),
                    hit.document_context,
                ))
        return tuple(intervening)

    def set_document_context(self, source_id: str, *, title: str, summary: str,
                             author_id: str, provenance: dict[str, object] | None = None) -> DocumentContext:
        """Replace document-level enrichment and refresh its passage index atomically."""
        if not isinstance(title, str) or not isinstance(summary, str):
            raise ValueError("title and summary must be strings")
        encoded = self._encode(provenance)
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute(
                """INSERT INTO document_context
                   (source_key, title, summary, author_id, updated_at, provenance)
                   VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(source_key) DO UPDATE SET
                   title = excluded.title, summary = excluded.summary,
                   author_id = excluded.author_id, updated_at = excluded.updated_at,
                   provenance = excluded.provenance""",
                (self._source_key(source_id), title, summary, author_id,
                 datetime.now(UTC).isoformat(), encoded),
            )
            context = self.read_document_context(source_id)
            assert context is not None
        return context

    def read_document_context(self, source_id: str) -> DocumentContext | None:
        """Read current enrichment; an ingested Source without context returns None."""
        row = self._db.execute(
            "SELECT title, summary, author_id, updated_at, provenance FROM document_context WHERE source_key = ?",
            (self._source_key(source_id),),
        ).fetchone()
        return self._decode_document_context(source_id, row)

    def create(self, kind: str, *, text: str, author_id: str,
               attributes: dict[str, object] | None = None,
               citations: Iterable[dict[str, object]] | None = None) -> Record:
        """Create a record; citations are resolved and linked supported_by in the same transaction."""
        _refuse_refs(text)
        attributes, source_ids = self._cite(attributes, citations)
        if kind == "Source":
            self._check_published(attributes)
        encoded = self._encode(attributes)
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            record = self._insert_record(kind, text, encoded, author_id)
            self._link_sources(record.id, source_ids, author_id)
        return record

    def ingest_source(self, *, description: str, document_text: str, author_id: str,
                      attributes: dict[str, object] | None = None, allow_duplicate: bool = False) -> Record:
        """Atomically create a Source record and its fixed canonical UTF-8 text.

        Text identical to an ingested Source is refused, naming that Source, unless allow_duplicate.
        """
        if not isinstance(document_text, str) or not document_text:
            raise ValueError("document_text must be a nonempty string")
        self._check_published(attributes)
        encoded = self._encode(attributes)
        content = document_text.encode("utf-8")
        digest = sha256(content).hexdigest()
        if type(allow_duplicate) is not bool:
            raise ValueError("allow_duplicate must be a boolean")
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")  # The duplicate check and insert share one write lock.
            existing = self._db.execute(
                """SELECT r.id FROM source_content s JOIN records r ON r.record_key = s.source_key
                   WHERE s.text_sha256 = ? ORDER BY s.source_key LIMIT 1""", (digest,)).fetchone()
            if existing and not allow_duplicate:
                raise ValueError(f"identical text is already ingested as Source {existing[0]}; "
                                 "reuse it, or pass allow_duplicate")
            source = self._insert_record("Source", description, encoded, author_id)
            self._db.execute(
                "INSERT INTO source_content (source_key, text_utf8, text_sha256) VALUES (?, ?, ?)",
                (self._record_key(source.id), content, digest),
            )
        return source

    def read_source_text(self, source_id: str, *, start_byte: int = 0,
                         end_byte: int | None = None) -> str:
        """Read canonical text or a half-open UTF-8 byte range, without normalization."""
        if type(start_byte) is not int or (end_byte is not None and type(end_byte) is not int):
            raise ValueError("byte offsets must be integers")
        source_key = self._source_key(source_id)
        with self._db.blobopen("source_content", "text_utf8", source_key, readonly=True) as content:
            end_byte = len(content) if end_byte is None else end_byte
            if not 0 <= start_byte <= end_byte <= len(content):
                raise ValueError("byte range is outside source text")
            for offset in (start_byte, end_byte):
                if offset < len(content):
                    content.seek(offset)
                    if content.read(1)[0] & 0xC0 == 0x80:
                        raise ValueError("byte offsets must be UTF-8 character boundaries")
            content.seek(start_byte)
            return content.read(end_byte - start_byte).decode("utf-8")

    def locate_source_text(self, source_id: str, quote: str, *,
                           limit: int = 10) -> tuple[SourceSegment, ...]:
        """Find exact, nonoverlapping occurrences of a quotation as UTF-8 byte ranges."""
        if not isinstance(quote, str) or not quote.strip():
            raise ValueError("quote must be a nonblank string")
        _check_limit(limit)
        needle = quote.encode("utf-8")
        with self._db.blobopen("source_content", "text_utf8", self._source_key(source_id),
                               readonly=True) as content:
            body = content.read()
        # A valid UTF-8 needle can only match whole characters in valid UTF-8 text.
        matches, start = [], body.find(needle)
        while start != -1 and len(matches) < limit:
            matches.append(SourceSegment(source_id, start, start + len(needle), quote))
            start = body.find(needle, start + len(needle))
        return tuple(matches)

    def chunk_source(self, source_id: str, *, max_chars: int = 2000) -> int:
        """Replace the source's passages using the default splitter; return their count."""
        ranges = split_ranges(self.read_source_text(source_id), max_chars=max_chars)
        return self.set_passages(source_id, ranges)

    def set_passages(self, source_id: str, ranges: Iterable[tuple[int, int]]) -> int:
        """Replace passages with a complete, nonoverlapping partition of source bytes."""
        source_key = self._source_key(source_id)
        ranges = tuple((start, end) for start, end in ranges)
        previous_end = 0
        with self._db.blobopen("source_content", "text_utf8", source_key, readonly=True) as content:
            for start, end in ranges:
                if type(start) is not int or type(end) is not int:
                    raise ValueError("passage offsets must be integers")
                if not previous_end == start < end <= len(content):
                    raise ValueError("passages must be ordered, contiguous, nonempty byte ranges")
                if end < len(content):
                    content.seek(end)
                    if content.read(1)[0] & 0xC0 == 0x80:
                        raise ValueError("passage boundaries must be UTF-8 character boundaries")
                previous_end = end
            if previous_end != len(content):
                raise ValueError("passages must cover the entire source text")
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute("DELETE FROM passages WHERE source_key = ?", (source_key,))
            self._db.executemany(
                "INSERT INTO passages (source_key, ordinal, start_byte, end_byte) VALUES (?, ?, ?, ?)",
                ((source_key, ordinal, start, end) for ordinal, (start, end) in enumerate(ranges)),
            )
        return len(ranges)

    def read_passages(self, source_id: str, *, start_ordinal: int = 0,
                      limit: int = 100) -> tuple[Passage, ...]:
        """Read current passage locators in source order, starting at an inclusive ordinal."""
        if type(start_ordinal) is not int or start_ordinal < 0:
            raise ValueError("start_ordinal must be a nonnegative integer")
        _check_limit(limit)
        rows = self._db.execute(
            """SELECT ordinal, start_byte, end_byte FROM passages
               WHERE source_key = ? AND ordinal >= ? ORDER BY ordinal LIMIT ?""",
            (self._source_key(source_id), start_ordinal, limit),
        ).fetchall()
        return tuple(Passage(source_id, **dict(row)) for row in rows)

    def update(self, record_id: str, *, text: str, author_id: str,
               attributes: dict[str, object] | None = None,
               citations: Iterable[dict[str, object]] | None = None) -> Record:
        """Replace contents in place; omitted attributes becomes an empty object.

        Citations replace attributes["citations"] and add missing supported_by links;
        existing links are kept.
        """
        _refuse_refs(text)
        attributes, source_ids = self._cite(attributes, citations)
        encoded = self._encode(attributes)
        current = self.read(record_id)  # A missing or unseen record is reported before anything is written.
        if self._value_changed(current, attributes or {}):
            return self._write_version(current, text=text, attributes=attributes or {}, source_ids=source_ids,
                                       author_id=author_id)
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            current = self.read(record_id)
            self._check_editable(current)
            self._check_contents(current, text, attributes or {})
            if current.kind == "Source":
                self._check_published(attributes)
            self._db.execute(
                """UPDATE records SET text = ?, attributes = ?, author_id = ?, updated_at = ?, as_of = ?
                   WHERE id = ?""",
                (text, encoded, author_id, datetime.now(UTC).isoformat(), self._stamp(), record_id),
            )
            self._link_sources(record_id, source_ids, author_id)
            record = self.read(record_id)
        return record

    @staticmethod
    def _value_changed(current: Record, attributes: dict[str, object]) -> bool:
        """A Variable's value changes when it had one and the new attributes give a different one. Setting a
        value where there was none is not a change."""
        return (current.kind == "Variable" and current.attributes.get("value") is not None
                and attributes.get("value") != current.attributes.get("value"))

    def _write_version(self, current: Record, *, text: str, attributes: dict[str, object],
                       source_ids: tuple[str, ...], author_id: str) -> Record:
        """A new version of a Variable: a record with the new contents, stamped with the day, linked `updates`
        the current one and carrying its outgoing links (its place in the model, what it rests on), so the
        old value stays true of its own date and a past decision can be reviewed."""
        encoded = self._encode(attributes)
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            self._check_contents(current, text, attributes)
            version = self._insert_record("Variable", text, encoded, author_id)
            self._link_sources(version.id, source_ids, author_id)
            self._insert_link(version.id, current.id, "updates", "The same quantity as of a later date.", author_id)
            for link in self._links_of(current.id, "out", None, 1000)[0]:
                if link.relation in ("updates", "supersedes"):
                    continue  # The old version's own history.
                values = self._link_values(version.id, link.to_id, link.relation, link.explanation, author_id)
                try:
                    self._refuse_duplicate_link(values)
                except ValueError:
                    continue  # Already added, as a citation's supported_by link is.
                self._db.execute(
                    f"""INSERT INTO links (from_key, to_key, relation, explanation, author_id, updated_at,
                        as_of, id, created_by, layer_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, {_WRITABLE_LAYER})""",
                    (*values, self._stamp(), str(uuid4()), author_id))
        return version

    def patch(self, record_id: str, *, author_id: str, text: str | None = None,
              attributes: dict[str, object] | None = None, remove: Iterable[str] = (),
              citations: Iterable[dict[str, object]] | None = None) -> Record:
        """Change only the given text and top-level attributes; the rest of the record is kept.

        The current record is read and rewritten in one transaction, so concurrent patches to
        different attributes are all kept. Removing an absent attribute changes nothing.
        """
        if attributes is not None and not isinstance(attributes, dict):
            raise ValueError("attributes must be a JSON object")
        if isinstance(remove, (str, bytes)):
            raise ValueError("remove must be a list of attribute names")
        remove = tuple(remove)
        if any(not isinstance(key, str) for key in remove):
            raise ValueError("remove must be a list of attribute names")
        if text is None and not attributes and not remove and citations is None:
            raise ValueError("patch needs text, attributes, remove or citations")
        _refuse_refs(text)
        changes, source_ids = self._cite(attributes or {}, citations)
        if set(changes) & set(remove):
            raise ValueError("an attribute cannot be both set and removed")
        current = self.read(record_id)
        merged = {key: value for key, value in current.attributes.items() if key not in remove} | changes
        if self._value_changed(current, merged):
            return self._write_version(current, text=current.text if text is None else text, attributes=merged,
                                       source_ids=source_ids, author_id=author_id)
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            current = self.read(record_id)
            self._check_editable(current)
            merged = {key: value for key, value in current.attributes.items() if key not in remove}
            self._check_contents(current, current.text if text is None else text, {**merged, **changes})
            if current.kind == "Source":
                self._check_published({**merged, **changes})
            self._db.execute(
                """UPDATE records SET text = ?, attributes = ?, author_id = ?, updated_at = ?, as_of = ?
                   WHERE id = ?""",
                (current.text if text is None else text, self._encode({**merged, **changes}),
                 author_id, datetime.now(UTC).isoformat(), self._stamp(), record_id),
            )
            self._link_sources(record_id, source_ids, author_id)
            record = self.read(record_id)
        return record

    def merge(self, keep_id: str, duplicate_id: str, *, author_id: str) -> Merge:
        """Fold a duplicate record into the one kept: every link of the duplicate is moved to the kept
        record (one the kept record already has is left on the duplicate), the duplicate's aliases and
        name join the kept record's aliases when it can be edited, and the duplicate is retired as
        superseded by the kept record. The duplicate must be in the workspace's own layer; the kept
        record may be anywhere. Each step is its own transaction, as with the commands it is built from.
        """
        keep, duplicate = self.read(keep_id), self.read(duplicate_id)
        if keep.id == duplicate.id:
            raise ValueError("the kept record and the duplicate are the same record")
        if keep.kind != duplicate.kind or keep.kind == "Source":
            raise ValueError("merge folds together two records of the same kind other than Source")
        if duplicate.attributes.get("status") == "superseded":
            raise ValueError(f"{duplicate.id} is already superseded")
        own = self.layers()[0].name
        if duplicate.layer != own:
            raise ValueError(f"the duplicate is in the read-only {duplicate.layer} layer; only a record in the "
                             f"writable {own} layer can be folded into another")
        moved = dropped = 0
        for direction, field in (("in", "to_id"), ("out", "from_id")):
            for link in self._links_of(duplicate.id, direction, None, 10000)[0]:
                other = link.from_id if direction == "in" else link.to_id
                if other == keep.id:
                    continue  # A link between the two is kept as it is.
                try:
                    self.patch_link(link.id, author_id=author_id, **{field: keep.id})
                    moved += 1
                except ValueError:
                    dropped += 1
        if keep.layer == own:
            aliases = keep.attributes.get("aliases") if isinstance(keep.attributes.get("aliases"), list) else []
            others = duplicate.attributes.get("aliases") if isinstance(duplicate.attributes.get("aliases"), list) else []
            name = duplicate.text.split(" (")[0].split(",")[0].strip()
            joined = [a for a in dict.fromkeys([*aliases, *others, name]) if isinstance(a, str) and a and a != keep.text]
            if joined != aliases:
                self.patch(keep.id, author_id=author_id, attributes={"aliases": joined})
        self.create_link(keep.id, duplicate.id, "supersedes", author_id=author_id,
                         explanation="Duplicate record for the same thing; its links were moved to this one.")
        self.patch(duplicate.id, author_id=author_id, attributes={
            "status": "superseded", "status_reason": f"Duplicate of {keep.kind} {keep.id}; its links were moved there."})
        return Merge(keep.id, duplicate.id, moved, dropped)

    def read_link(self, link_id: str) -> Link:
        row = self._db.execute(_LINK_SELECT + f" WHERE l.id = ? AND {self._seen('l', link=True)}", (link_id,)).fetchone()
        if row is None:
            raise self._missing(link_id, link=True)
        return Link(**dict(row))

    def create_link(self, from_id: str, to_id: str, relation: str, *, author_id: str,
                    explanation: str = "") -> Link:
        _refuse_refs(explanation)
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            link = self._insert_link(from_id, to_id, relation, explanation, author_id)
        return link

    def contribute(self, *, author_id: str, records: Iterable[dict[str, object]] = (),
                   links: Iterable[dict[str, object]] = ()) -> Contribution:
        """Create records and links in one transaction.

        Links may name new records by ref, and record text and link explanations may name them as
        {ref:NAME}, which is replaced by the new record's ID.
        """
        records = self._batch(records, "records", {"ref", "kind", "text"}, {"attributes", "citations"})
        links = self._batch(links, "links", {"from_id", "to_id", "relation"}, {"explanation"})
        refs = [record["ref"] for record in records]
        if any(not isinstance(ref, str) or not ref.strip() for ref in refs) or len(set(refs)) != len(refs):
            raise ValueError("record refs must be unique, nonblank strings")
        ids = {ref: str(uuid4()) for ref in refs}  # Known before writing, so text can name any new record.
        prepared = []
        for index, record in enumerate(records):
            where = f"records[{index}] (ref {record['ref']!r})"
            try:
                attributes, source_ids = self._cite(record.get("attributes"), record.get("citations"))
            except ValueError as error:
                raise ValueError(f"{where}: {error}") from None
            prepared.append((record, _fill_refs(record["text"], ids, where), self._encode(attributes), source_ids))
        explanations = [_fill_refs(link.get("explanation", ""), ids, f"links[{index}]")
                        for index, link in enumerate(links)]
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            if refs and self._db.execute(f"SELECT 1 FROM records WHERE id IN ({','.join('?' * len(refs))})",
                                         refs).fetchone():
                raise ValueError("record refs must not be existing record IDs")
            created = {}
            for record, text, encoded, source_ids in prepared:
                created[record["ref"]] = self._insert_record(record["kind"], text, encoded, author_id,
                                                             ids[record["ref"]])
                self._link_sources(created[record["ref"]].id, source_ids, author_id)
            made = []
            for index, (link, explanation) in enumerate(zip(links, explanations)):
                try:
                    made.append(self._insert_link(ids.get(link["from_id"], link["from_id"]),
                                                  ids.get(link["to_id"], link["to_id"]), link["relation"],
                                                  explanation, author_id))
                except ValueError as error:
                    raise ValueError(f"links[{index}]: {error}") from None
        return Contribution(created, tuple(made))

    def update_link(self, link_id: str, *, from_id: str, to_id: str, relation: str,
                    author_id: str, explanation: str = "") -> Link:
        """Replace current link contents and refresh its last-edited timestamp."""
        _refuse_refs(explanation)
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            current = self.read_link(link_id)  # A missing link is reported before its contents are checked.
            self._check_editable(current)
            self._check_link_change(current, relation, from_id, to_id)
            values = self._link_values(from_id, to_id, relation, explanation, author_id)
            self._refuse_duplicate_link(values, link_id)
            self._db.execute(
                """UPDATE links SET from_key = ?, to_key = ?, relation = ?, explanation = ?,
                   author_id = ?, updated_at = ?, as_of = ? WHERE id = ?""",
                (*values, self._stamp(), link_id),
            )
            link = self.read_link(link_id)
        return link

    def patch_link(self, link_id: str, *, author_id: str, from_id: str | None = None,
                   to_id: str | None = None, relation: str | None = None,
                   explanation: str | None = None) -> Link:
        """Change only the given link fields in one transaction; the rest of the link is kept."""
        if from_id is None and to_id is None and relation is None and explanation is None:
            raise ValueError("patch_link needs from_id, to_id, relation or explanation")
        _refuse_refs(explanation)
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            current = self.read_link(link_id)
            self._check_editable(current)
            self._check_link_change(current, current.relation if relation is None else relation,
                                    current.from_id if from_id is None else from_id,
                                    current.to_id if to_id is None else to_id)
            values = self._link_values(current.from_id if from_id is None else from_id,
                                       current.to_id if to_id is None else to_id,
                                       current.relation if relation is None else relation,
                                       current.explanation if explanation is None else explanation, author_id)
            self._refuse_duplicate_link(values, link_id)
            self._db.execute(
                """UPDATE links SET from_key = ?, to_key = ?, relation = ?, explanation = ?,
                   author_id = ?, updated_at = ?, as_of = ? WHERE id = ?""",
                (*values, self._stamp(), link_id),
            )
            link = self.read_link(link_id)
        return link

    def connections(self, record_id: str, *, direction: str = "both",
                    relation: str | None = None, limit: int = 100) -> Connections:
        """Current one-hop links and the records they reach, in link creation order.

        has_more reports truncation.
        """
        self._check_walk(direction, limit)
        links, has_more = self._links_of(record_id, direction, None if relation is None else (relation,), limit)
        # Records are never deleted, so every linked record can be read.
        ids = dict.fromkeys(link.from_id if link.to_id == record_id else link.to_id for link in links)
        found = {row["id"]: self._decode_record(row) for row in self._db.execute(
            _RECORD_SELECT + f" WHERE r.id IN (SELECT value FROM json_each(?)) AND {self._seen('r')}",
            (json.dumps(list(ids)),))}
        return Connections(links, has_more, {record: found[record] for record in ids})

    def expand(self, seed_ids: Iterable[str], *, depth: int = 1, direction: str = "both",
               relations: Iterable[str] | None = None, limit: int = 100,
               per_record_limit: int = 50) -> Expansion:
        """Walk current links breadth-first from seed records, at most two hops, in one snapshot.

        Each record is reported once, at its shortest depth, with the first path found. At most
        per_record_limit links to records not yet reached are followed from any one record; records
        with more are listed as capped.
        """
        if isinstance(seed_ids, (str, bytes)):
            raise ValueError("seed_ids must be a list of record IDs")
        seeds = tuple(dict.fromkeys(seed_ids))
        if not 1 <= len(seeds) <= 100 or any(not isinstance(seed, str) for seed in seeds):
            raise ValueError("seed_ids must contain 1 to 100 record IDs")
        if type(depth) is not int or depth not in (1, 2):
            raise ValueError("depth must be 1 or 2")
        if relations is not None:
            if isinstance(relations, (str, bytes)):
                raise ValueError("relations must be a list of relation names")
            relations = tuple(relations)
            if not relations or any(not isinstance(name, str) or not name.strip() for name in relations):
                raise ValueError("relations must be a nonempty list of relation names")
        self._check_walk(direction, limit)
        _check_limit(per_record_limit, "per_record_limit")
        with self._db:
            self._db.execute("BEGIN")  # One read snapshot for the whole walk.
            reached = {seed: Reached(self.read(seed), 0, ()) for seed in seeds}
            frontier, capped, has_more = list(seeds), [], False
            for level in range(1, depth + 1):
                following = []
                for node in frontier:
                    links, more = self._links_of(node, direction, relations, per_record_limit, exclude=reached)
                    if more:
                        capped.append(node)
                    for link in links:
                        other = link.from_id if link.to_id == node else link.to_id
                        if other in reached:  # Reached earlier in this level.
                            continue
                        if len(reached) - len(seeds) >= limit:
                            has_more = True
                            break
                        reached[other] = Reached(self.read(other), level, reached[node].path + (link,))
                        following.append(other)
                    if has_more:
                        break
                if has_more:
                    break
                frontier = following
        return Expansion(tuple(reached.values()), tuple(capped), has_more)

    @staticmethod
    def _check_walk(direction: str, limit: int) -> None:
        if direction not in ("out", "in", "both"):
            raise ValueError("direction must be 'out', 'in' or 'both'")
        _check_limit(limit)

    def _links_of(self, record_id: str, direction: str, relations: tuple[str, ...] | None,
                  limit: int, exclude: Iterable[str] = ()) -> tuple[tuple[Link, ...], bool]:
        """Links touching a record in creation order, and whether more than limit matched.

        Links whose other end is in exclude are skipped before the limit applies.
        """
        key = self._record_key(record_id)
        where = {"out": "l.from_key = ?", "in": "l.to_key = ?", "both": "(l.from_key = ? OR l.to_key = ?)"}
        parameters: list[object] = [key, key] if direction == "both" else [key]
        sql = _LINK_SELECT + f" WHERE {where[direction]} AND {self._seen('l', link=True)}"
        if relations is not None:
            sql += " AND l.relation IN (SELECT value FROM json_each(?))"
            parameters.append(json.dumps(relations))
        if exclude:
            sql += " AND (CASE WHEN l.from_key = ? THEN t.id ELSE f.id END) NOT IN (SELECT value FROM json_each(?))"
            parameters += [key, json.dumps(list(exclude))]
        rows = self._db.execute(sql + " ORDER BY l.link_key LIMIT ?", (*parameters, limit + 1)).fetchall()
        return tuple(Link(**dict(row)) for row in rows[:limit]), len(rows) > limit

    def list_records(self, *, kind: str | None = None, author_id: str | None = None,
                     created_by: str | None = None, since: str | None = None,
                     where: dict[str, str | int | float | bool | None] | None = None,
                     after: str | None = None, limit: int = 100) -> RecordPage:
        """Current records in creation order, optionally filtered; pass the last ID as after to continue.

        where matches top-level attributes exactly: every named key must be present with that
        JSON scalar value. since keeps records created or edited at or after a UTC ISO timestamp
        or date, such as "2026-09-28" or "2026-09-28T18:00".
        """
        _check_kind(kind)
        for name, value in (("author_id", author_id), ("created_by", created_by), ("since", since)):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be a string or None")
        _check_limit(limit)
        if where is not None and not isinstance(where, dict):
            raise ValueError("where must be an object of attribute values")
        conditions, values = [], []
        for key, value in (where or {}).items():
            if not isinstance(key, str) or not key or '"' in key:
                raise ValueError("where keys must be nonempty attribute names without quotes")
            if not (value is None or isinstance(value, (str, int, float, bool))) or (
                    isinstance(value, float) and not math.isfinite(value)):
                raise ValueError("where values must be strings, finite numbers, booleans or null")
            # Match the JSON type as well as the value, so 1, 1.0, true and "1" stay distinct.
            path = f'$."{key}"'
            if value is None or isinstance(value, bool):
                conditions.append("json_type(r.attributes, ?) = ?")
                values += [path, "null" if value is None else str(value).lower()]
            else:
                conditions.append("json_type(r.attributes, ?) = ? AND json_extract(r.attributes, ?) = ?")
                values += [path, {str: "text", int: "integer", float: "real"}[type(value)], path, value]
        start = 0 if after is None else self._record_key(after)
        rows = self._db.execute(
            _RECORD_SELECT + f"""WHERE r.record_key > ? AND (? IS NULL OR r.kind = ?)
                 AND (? IS NULL OR r.author_id = ?) AND (? IS NULL OR r.created_by = ?)
                 AND (? IS NULL OR r.updated_at >= ?) AND {self._seen('r')}
               {"".join(" AND " + condition for condition in conditions)}
               ORDER BY r.record_key LIMIT ?""",
            (start, kind, kind, author_id, author_id, created_by, created_by, since, since, *values, limit + 1),
        ).fetchall()
        return RecordPage(tuple(self._decode_record(row) for row in rows[:limit]), len(rows) > limit)

    def list_links(self, *, relation: str | None = None, author_id: str | None = None,
                   created_by: str | None = None, since: str | None = None,
                   after: str | None = None, limit: int = 100) -> LinkPage:
        """Current links in creation order, optionally filtered; pass the last link ID as after to continue.

        since keeps links created or edited at or after a UTC ISO timestamp or date, as in list_records.
        """
        for name, value in (("relation", relation), ("author_id", author_id), ("created_by", created_by),
                            ("since", since)):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be a string or None")
        _check_limit(limit)
        start = 0 if after is None else self._db.execute(
            "SELECT link_key FROM links WHERE id = ?", (self.resolve_id(after, link=True),)).fetchone()[0]
        rows = self._db.execute(
            _LINK_SELECT + f""" WHERE l.link_key > ? AND (? IS NULL OR l.relation = ?)
                AND (? IS NULL OR l.author_id = ?) AND (? IS NULL OR l.created_by = ?)
                AND (? IS NULL OR l.updated_at >= ?) AND {self._seen('l', link=True)}
                ORDER BY l.link_key LIMIT ?""",
            (start, relation, relation, author_id, author_id, created_by, created_by, since, since, limit + 1),
        ).fetchall()
        return LinkPage(tuple(Link(**dict(row)) for row in rows[:limit]), len(rows) > limit)

    def cited_by(self, source_id: str, *, start_byte: int = 0, end_byte: int | None = None,
                 limit: int = 100) -> CitationPage:
        """Records whose citations overlap a half-open byte range of a source, in source order."""
        if type(start_byte) is not int or (end_byte is not None and type(end_byte) is not int):
            raise ValueError("byte offsets must be integers")
        _check_limit(limit)
        source_key = self._source_key(source_id)
        length = self._db.execute("SELECT length(text_utf8) FROM source_content WHERE source_key = ?",
                                  (source_key,)).fetchone()[0]
        end_byte = length if end_byte is None else min(end_byte, length)  # "To the end" needs no length.
        if not 0 <= start_byte < end_byte:
            raise ValueError("byte range is outside source text")
        rows = self._db.execute(
            f"""SELECT r.id, r.kind, r.text, r.attributes, r.author_id, r.created_by, r.updated_at, r.as_of,
                      y.name AS layer, c.start_byte AS cited_start, c.end_byte AS cited_end
               FROM citations c JOIN records r ON r.record_key = c.record_key
               JOIN layers y ON y.layer_key = r.layer_key
               WHERE c.source_key = ? AND c.start_byte < ? AND c.end_byte > ? AND {self._seen('r')}
               ORDER BY c.start_byte, c.end_byte, c.record_key, c.ordinal LIMIT ?""",
            (source_key, end_byte, start_byte, limit + 1),
        ).fetchall()
        citations = tuple(Citation(Record(row["id"], row["kind"], row["text"], json.loads(row["attributes"]),
                                          row["author_id"], row["created_by"], row["updated_at"], row["as_of"],
                                          row["layer"]),
                                   source_id, row["cited_start"], row["cited_end"]) for row in rows[:limit])
        return CitationPage(citations, len(rows) > limit)

    def relation_usage(self) -> tuple[RelationUsage, ...]:
        """Count current links by relation and the kinds of record they connect."""
        rows = self._db.execute(
            f"""SELECT l.relation, f.kind AS from_kind, t.kind AS to_kind, count(*) AS links
               FROM links l JOIN records f ON f.record_key = l.from_key
               JOIN records t ON t.record_key = l.to_key WHERE {self._seen('l', link=True)}
               GROUP BY l.relation, f.kind, t.kind ORDER BY l.relation, links DESC, f.kind, t.kind""")
        return tuple(RelationUsage(**dict(row)) for row in rows)

    def _cite(self, attributes: dict[str, object] | None,
              citations: Iterable[dict[str, object]] | None) -> tuple[dict[str, object] | None, tuple[str, ...]]:
        """Resolve citations to exact byte ranges with their quotes; return attributes and cited sources."""
        if citations is None:
            return attributes, ()
        if attributes is not None and not isinstance(attributes, dict):
            raise ValueError("attributes must be a JSON object")
        if attributes and "citations" in attributes:
            raise ValueError("supply citations as an argument or an attribute, not both")
        if isinstance(citations, (str, bytes, dict)):
            raise ValueError("citations must be a list of objects")
        resolved = []
        for index, citation in enumerate(citations):
            try:
                resolved.append(self._resolve_citation(citation))
            except ValueError as error:
                raise ValueError(f"citations[{index}]: {error}") from None
        return {**(attributes or {}), "citations": resolved}, tuple(dict.fromkeys(c["source_id"] for c in resolved))

    def _resolve_citation(self, citation: object) -> dict[str, object]:
        if (not isinstance(citation, dict) or "source_id" not in citation
                or not set(citation) <= {"source_id", "quote", "start_byte", "end_byte"}):
            raise ValueError("each citation needs source_id and a quote or start_byte/end_byte")
        source_id, quote = citation["source_id"], citation.get("quote")
        if isinstance(source_id, str):
            source_id = self.resolve_id(source_id)  # Citations always store the full Source ID.
        start, end = citation.get("start_byte"), citation.get("end_byte")
        if quote is None:
            if start is None or end is None:
                raise ValueError("a citation without a quote needs start_byte and end_byte")
            quote = self.read_source_text(source_id, start_byte=start, end_byte=end)
            if not quote.strip():
                raise ValueError("a cited range must contain text")
        else:
            matches = [m for m in self.locate_source_text(source_id, quote, limit=1000)
                       if start in (None, m.start_byte) and end in (None, m.end_byte)]
            if len(matches) != 1:
                offsets = ", ".join(str(m.start_byte) for m in matches[:10])
                where = "" if start is None and end is None else " at the given offsets"
                raise ValueError(f"quote not found in source {source_id}{where}{self._near_quote(source_id, quote)}"
                                 if not matches else f"quote occurs {len(matches)} times in source {source_id} "
                                 f"(start_byte {offsets}); add start_byte to choose one")
            start, end = matches[0].start_byte, matches[0].end_byte
        return {"source_id": source_id, "start_byte": start, "end_byte": end, "quote": quote}

    def _near_quote(self, source_id: str, quote: str) -> str:
        """A hint naming source text that differs from the quote only in spacing, dashes or quote marks."""
        pattern = ""
        for part in re.findall(r"\s+|.", quote, re.S):
            group = next((g for g in _LOOKALIKES if part in g), None)
            pattern += r"\s+" if part.isspace() else f"[{re.escape(group)}]" if group else re.escape(part)
        text = self.read_source_text(source_id)
        match = re.search(pattern, text)
        if match is None:
            return ""
        return (f"; the source has {match.group()!r} at start_byte {len(text[:match.start()].encode('utf-8'))}, "
                "which differs only in spacing, dashes or quote marks; quote the source exactly")

    def _link_sources(self, record_id: str, source_ids: tuple[str, ...], author_id: str) -> None:
        """Add a supported_by link to each cited source that is not already linked."""
        for source_id in source_ids:
            values = self._link_values(record_id, source_id, "supported_by", "Cited evidence.", author_id)
            if self._db.execute("""SELECT 1 FROM links WHERE from_key = ? AND to_key = ?
                                   AND relation = 'supported_by'""", values[:2]).fetchone() is None:
                self._db.execute(
                    f"""INSERT INTO links (from_key, to_key, relation, explanation, author_id, updated_at,
                        as_of, id, created_by, layer_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, {_WRITABLE_LAYER})""",
                    (*values, self._stamp(), str(uuid4()), author_id),
                )

    def _insert_link(self, from_id: str, to_id: str, relation: str, explanation: str, author_id: str) -> Link:
        link_id = str(uuid4())
        values = self._link_values(from_id, to_id, relation, explanation, author_id)
        self._refuse_duplicate_link(values)
        if relation == "defined_by":
            self._check_defined_by(from_id, to_id)
        self._db.execute(
            f"""INSERT INTO links (from_key, to_key, relation, explanation, author_id, updated_at,
                as_of, id, created_by, layer_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, {_WRITABLE_LAYER})""",
            (*values, self._stamp(), link_id, author_id),
        )
        return self.read_link(link_id)

    def _feature_of(self, record_id: str) -> Record | None:
        """The Feature a Variable is a value of, if any."""
        row = self._db.execute("""SELECT f.id FROM links l JOIN records f ON f.record_key = l.to_key
                                  WHERE l.from_key = ? AND l.relation = 'defined_by'""",
                               (self._record_key(record_id),)).fetchone()
        return None if row is None else self.read(row[0])

    def _check_defined_by(self, from_id: str, to_id: str) -> None:
        value, feature = self.read(self.resolve_id(from_id)), self.read(self.resolve_id(to_id))
        if value.kind != "Variable" or feature.kind != "Feature":
            raise ValueError("defined_by links a Variable to the Feature it is a value of")
        if (existing := self._feature_of(value.id)) is not None:
            raise ValueError(f"{value.id} is already a value of Feature {existing.id}")
        _check_value(value.attributes, feature)

    def _check_contents(self, current: Record, text: str, attributes: dict[str, object]) -> None:
        """New contents for a value of a Feature stay within it, and a Feature that values rest on keeps its
        instructions and definition, so each value stays tied to the instructions it was made under."""
        if current.kind == "Variable" and (feature := self._feature_of(current.id)) is not None:
            _check_value(attributes, feature)
        if current.kind != "Feature":
            return
        _check_definition(attributes)
        if (text != current.text or any(attributes.get(k) != current.attributes.get(k) for k in _DEFINING)) and \
                self._db.execute("SELECT 1 FROM links WHERE to_key = ? AND relation = 'defined_by'",
                                 (self._record_key(current.id),)).fetchone():
            raise ValueError(f"values rest on Feature {current.id}, so its instructions and definition are fixed; "
                             "write the new ones as a new Feature linked updates this one")

    def _check_link_change(self, current: Link, relation: str, from_id: str, to_id: str) -> None:
        """Which Feature a value belongs to is fixed: a defined_by link changes only its explanation."""
        if "defined_by" in (current.relation, relation) and (relation, self.resolve_id(from_id), self.resolve_id(to_id)) \
                != (current.relation, current.from_id, current.to_id):
            raise ValueError("a defined_by link changes only its explanation; write a new value of the other Feature")

    def _refuse_duplicate_link(self, values: tuple, link_id: str | None = None) -> None:
        """One link per endpoints and relation; a second would only repeat or split its explanation."""
        row = self._db.execute("SELECT id FROM links WHERE from_key = ? AND to_key = ? AND relation = ? AND id IS NOT ?",
                               (*values[:3], link_id)).fetchone()
        if row is not None:
            raise ValueError(f"an identical {values[2]} link already exists: {row[0]}; patch it instead")

    @staticmethod
    def _batch(items: Iterable[dict[str, object]], name: str, required: set[str],
               optional: set[str]) -> list[dict[str, object]]:
        if isinstance(items, (str, bytes, dict)):
            raise ValueError(f"{name} must be a list of objects")
        items = list(items)
        if len(items) > 1000:
            raise ValueError(f"{name} can contain at most 1,000 items")
        for index, item in enumerate(items):
            if not isinstance(item, dict) or not required <= set(item) <= required | optional:
                raise ValueError(f"{name}[{index}] needs {', '.join(sorted(required))}"
                                 f" and may have {', '.join(sorted(optional))}")
        return items

    def _record_key(self, record_id: str) -> int:
        """Key for an exact ID or a unique prefix of 8+ characters, so payload IDs can be short."""
        query = f"SELECT record_key FROM records r WHERE r.id = ? AND {self._seen('r')}"
        row = self._db.execute(query, (record_id,)).fetchone()
        if row is None:
            if not isinstance(record_id, str):
                raise KeyError(record_id)
            row = self._db.execute(query, (self.resolve_id(record_id),)).fetchone()
        return row[0]

    def _source_key(self, source_id: str) -> int:
        query = f"""SELECT s.source_key FROM source_content s
                    JOIN records r ON r.record_key = s.source_key WHERE r.id = ? AND {self._seen('r')}"""
        row = self._db.execute(query, (source_id,)).fetchone()
        if row is None and isinstance(source_id, str) and len(source_id) >= 8:
            row = self._db.execute(query, (self.resolve_id(source_id),)).fetchone()
        if row is None:
            raise self._missing(source_id)
        return row[0]

    def _insert_record(self, kind: str, text: str, encoded: str, author_id: str,
                       record_id: str | None = None) -> Record:
        if kind == "Feature":
            _check_definition(json.loads(encoded))
        record_id = record_id or str(uuid4())
        self._db.execute(
            f"""INSERT INTO records (id, kind, text, attributes, author_id, created_by, updated_at, as_of, layer_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, {_WRITABLE_LAYER})""",
            (record_id, kind, text, encoded, author_id, author_id, datetime.now(UTC).isoformat(), self._stamp()),
        )
        return self.read(record_id)

    def _link_values(self, from_id: str, to_id: str, relation: str,
                     explanation: str, author_id: str) -> tuple:
        return (self._record_key(from_id), self._record_key(to_id), relation,
                explanation, author_id, datetime.now(UTC).isoformat())

    def _decode_document_context(self, source_id: str, row: sqlite3.Row | None) -> DocumentContext | None:
        # A summary is written on the day it is generated, whatever the document's date, so a cutoff hides it.
        if row is None or row["title"] is None or self.as_of is not None:
            return None
        return DocumentContext(source_id, row["title"], row["summary"], row["author_id"],
                               row["updated_at"], json.loads(row["provenance"]))

    @staticmethod
    def _search_expression(query: str) -> str:
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        words = dict.fromkeys(re.findall(r"[^\W_]+", unicodedata.normalize("NFC", query)))
        return " OR ".join(f'"{word}"' for word in words)

    @staticmethod
    def _decode_record(row: sqlite3.Row) -> Record:
        values = dict(row)
        values["attributes"] = json.loads(values["attributes"])
        return Record(**values)

    @staticmethod
    def _encode(attributes: dict[str, object] | None) -> str:
        attributes = {} if attributes is None else attributes
        if not isinstance(attributes, dict):
            raise ValueError("attributes must be a JSON object")
        encoded = json.dumps(attributes, allow_nan=False)
        # Reject silent JSON coercions such as integer keys or tuple values.
        if json.loads(encoded) != attributes:
            raise ValueError("attributes must use JSON types and string keys")
        return encoded
