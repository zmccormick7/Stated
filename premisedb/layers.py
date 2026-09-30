"""Publish a workspace's own layer as a database file, and attach published files to a workspace.

A published file is an ordinary PremiseDB database holding one sealed (read-only) layer, so every
command that reads a workspace also reads a published layer. Its `layers` row is the manifest.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class LayerExport:
    path: str
    name: str
    version: str
    records: int
    links: int
    sources: int  # Sources whose text the file carries.
    withheld: int  # Sources published without their text (attributes.redistributable is false).
    content_sha256: str


@dataclass(frozen=True)
class LayerAttachment:
    name: str
    version: str
    content_sha256: str
    records: dict[str, int]  # added, updated, removed and kept (dropped upstream but still linked here).
    links: dict[str, int]  # added, updated and removed.
    review: dict[str, list[str]] = field(default_factory=dict)  # What the workspace's own layer should reassess.


@dataclass(frozen=True)
class _Snapshot:
    records: list
    links: list
    contents: list  # id, text_utf8 (None when withheld), text_sha256, withheld.
    passages: list
    contexts: list

    def digest(self) -> str:
        digest = sha256()
        for table in (self.records, self.links, self.passages, self.contexts):
            for row in table:  # Link rows end with layer keys, which are local to a database.
                digest.update(json.dumps(list(row)[:8], ensure_ascii=False).encode("utf-8") + b"\n")
        for row in self.contents:  # Text is identified by its hash whether or not the file carries it.
            digest.update(json.dumps([row["id"], row["text_sha256"], bool(row["withheld"])]).encode("utf-8") + b"\n")
        return digest.hexdigest()


def _snapshot(db: sqlite3.Connection, key: int) -> _Snapshot:
    """Read one layer's rows in a consistent order, with Source text withheld where marked."""
    db.execute("BEGIN")
    try:
        records = db.execute(
            """SELECT id, kind, text, attributes, author_id, created_by, updated_at FROM records
               WHERE layer_key = ? ORDER BY record_key""", (key,)).fetchall()
        links = db.execute(
            """SELECT l.id, f.id AS from_id, t.id AS to_id, l.relation, l.explanation, l.author_id,
                      l.created_by, l.updated_at, f.layer_key AS from_layer, t.layer_key AS to_layer
               FROM links l JOIN records f ON f.record_key = l.from_key JOIN records t ON t.record_key = l.to_key
               WHERE l.layer_key = ? ORDER BY l.link_key""", (key,)).fetchall()
        contents = db.execute(
            """SELECT r.id, s.text_utf8, s.text_sha256,
                      json_type(r.attributes, '$.redistributable') = 'false' AS withheld
               FROM source_content s JOIN records r ON r.record_key = s.source_key WHERE r.layer_key = ?
               UNION ALL
               SELECT r.id, NULL, w.text_sha256, 1
               FROM withheld_sources w JOIN records r ON r.record_key = w.source_key WHERE r.layer_key = ?
               ORDER BY 1""", (key, key)).fetchall()
        withheld = {row["id"] for row in contents if row["withheld"]}
        # Passages and context describe text the file does not carry; a workspace supplying the text rebuilds them.
        passages = [row for row in db.execute(
            """SELECT r.id, p.ordinal, p.start_byte, p.end_byte FROM passages p
               JOIN records r ON r.record_key = p.source_key
               WHERE r.layer_key = ? ORDER BY r.id, p.ordinal""", (key,)) if row["id"] not in withheld]
        contexts = [row for row in db.execute(
            """SELECT r.id, c.title, c.summary, c.author_id, c.updated_at, c.provenance FROM document_context c
               JOIN records r ON r.record_key = c.source_key WHERE r.layer_key = ? ORDER BY r.id""", (key,))
            if row["id"] not in withheld]
    finally:
        db.execute("COMMIT")
    return _Snapshot(records, links, contents, passages, contexts)


def export_layer(store, path: str | Path, *, version: str) -> LayerExport:
    from .store import RecordStore  # The published file is created through the ordinary store.

    if not isinstance(version, str) or not version.strip():
        raise ValueError("version must be a nonblank string")
    path = Path(path)
    if path.exists():
        raise ValueError(f"{path} already exists; published files are never overwritten")
    own = store._db.execute("SELECT layer_key, name FROM layers WHERE writable = 1").fetchone()
    if own is None:
        raise ValueError("this database has no writable layer to export")
    key, name = own["layer_key"], own["name"]
    snapshot = _snapshot(store._db, key)
    outside = sum(row["from_layer"] != key or row["to_layer"] != key for row in snapshot.links)
    if outside:
        raise ValueError(f"{outside} links reach records in other layers; "
                         "exporting a layer that builds on attached layers is not supported yet")
    content_sha256 = snapshot.digest()
    with RecordStore(path, layer=name) as out:
        with out._db:
            out._db.execute("BEGIN IMMEDIATE")
            _write_rows(out._db, 1, snapshot)
            out._db.execute("UPDATE layers SET writable = 0, version = ?, content_sha256 = ?, exported_at = ?",
                            (version, content_sha256, datetime.now(UTC).isoformat()))
    withheld = sum(bool(row["withheld"]) for row in snapshot.contents)
    return LayerExport(str(path), name, version, len(snapshot.records), len(snapshot.links),
                       len(snapshot.contents) - withheld, withheld, content_sha256)


def _write_rows(db: sqlite3.Connection, key: int, snapshot: _Snapshot) -> None:
    """Insert a snapshot into an empty layer whose key is `key`, verbatim."""
    key_of = "(SELECT record_key FROM records WHERE id = ?)"
    db.executemany(
        f"""INSERT INTO records (id, kind, text, attributes, author_id, created_by, updated_at, layer_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, {key})""", (tuple(row) for row in snapshot.records))
    db.executemany(f"INSERT INTO source_content (source_key, text_utf8, text_sha256) VALUES ({key_of}, ?, ?)",
                   ((row["id"], row["text_utf8"], row["text_sha256"]) for row in snapshot.contents
                    if not row["withheld"]))
    db.executemany(f"INSERT INTO withheld_sources (source_key, text_sha256) VALUES ({key_of}, ?)",
                   ((row["id"], row["text_sha256"]) for row in snapshot.contents if row["withheld"]))
    # Citations were indexed before their sources' text existed; the update trigger re-derives them.
    db.execute(f"""UPDATE records SET attributes = attributes
                   WHERE layer_key = {key} AND json_type(attributes, '$.citations') = 'array'""")
    db.executemany(f"INSERT INTO passages (source_key, ordinal, start_byte, end_byte) VALUES ({key_of}, ?, ?, ?)",
                   (tuple(row) for row in snapshot.passages))
    db.executemany(
        f"""INSERT INTO document_context (source_key, title, summary, author_id, updated_at, provenance)
            VALUES ({key_of}, ?, ?, ?, ?, ?)""", (tuple(row) for row in snapshot.contexts))
    db.executemany(
        f"""INSERT INTO links (id, from_key, to_key, relation, explanation, author_id, created_by,
            updated_at, layer_key) VALUES (?, {key_of}, {key_of}, ?, ?, ?, ?, ?, {key})""",
        (tuple(row)[:8] for row in snapshot.links))


def attach_layer(store, path: str | Path) -> LayerAttachment:
    """Attach a published file as a read-only layer, or bring an attached layer to the file's version.

    Records keep their keys across versions, so the workspace's links and citations to them stay valid.
    Records the new version no longer holds are removed unless the workspace still links to them.
    """
    from .store import _SCHEMA_VERSION

    path = Path(path)
    if not path.is_file():
        raise ValueError(f"{path} is not a file")
    published = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
    published.row_factory = sqlite3.Row
    try:
        if published.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION:
            raise ValueError(f"{path} does not use schema format {_SCHEMA_VERSION}")
        manifests = published.execute(
            "SELECT layer_key, name, version, writable, content_sha256 FROM layers").fetchall()
        if len(manifests) != 1 or manifests[0]["writable"] or not manifests[0]["content_sha256"]:
            raise ValueError(f"{path} is not a published layer file: it must hold exactly one sealed layer")
        manifest = manifests[0]
        snapshot = _snapshot(published, manifest["layer_key"])
    finally:
        published.close()
    if snapshot.digest() != manifest["content_sha256"]:
        raise ValueError(f"{path} does not match its manifest hash; the file is altered or corrupt")
    name, version = manifest["name"], manifest["version"]

    db = store._db
    with db:
        db.execute("BEGIN IMMEDIATE")
        own = db.execute("SELECT layer_key, name FROM layers WHERE writable = 1").fetchone()
        if own is None or own["name"] == name:
            raise ValueError(f"a layer named {name!r} is this workspace's own layer")
        existing = db.execute("SELECT layer_key FROM layers WHERE name = ?", (name,)).fetchone()
        if existing is None:
            key = db.execute("INSERT INTO layers (name, version, writable) VALUES (?, ?, 0)",
                             (name, version)).lastrowid
        else:
            key = existing["layer_key"]
        ids = json.dumps([row["id"] for row in snapshot.records])
        elsewhere = db.execute(
            """SELECT count(*) FROM records WHERE layer_key != ? AND id IN (SELECT value FROM json_each(?))""",
            (key, ids)).fetchone()[0]
        if elsewhere:
            raise ValueError(f"{elsewhere} records in {path} already exist in other layers of this workspace")
        # The attached layer accepts writes only inside this transaction.
        db.execute("UPDATE layers SET writable = 0")
        db.execute("UPDATE layers SET writable = 1 WHERE layer_key = ?", (key,))
        try:
            result = _apply(db, key, own["layer_key"], snapshot) if existing else _first(db, key, snapshot)
            db.execute("UPDATE layers SET version = ?, content_sha256 = ?, exported_at = ? WHERE layer_key = ?",
                       (version, manifest["content_sha256"], datetime.now(UTC).isoformat(), key))
        finally:
            db.execute("UPDATE layers SET writable = 0")
            db.execute("UPDATE layers SET writable = 1 WHERE layer_key = ?", (own["layer_key"],))
        # The workspace's citations of this layer's sources follow the text now available.
        db.execute("""UPDATE records SET attributes = attributes
                      WHERE layer_key = ? AND json_type(attributes, '$.citations') = 'array'""", (own["layer_key"],))
    return LayerAttachment(name, version, manifest["content_sha256"], *result)


def _first(db: sqlite3.Connection, key: int, snapshot: _Snapshot):
    _write_rows(db, key, snapshot)
    return ({"added": len(snapshot.records), "updated": 0, "removed": 0, "kept": 0},
            {"added": len(snapshot.links), "updated": 0, "removed": 0}, {})


def _apply(db: sqlite3.Connection, key: int, own_key: int, snapshot: _Snapshot):
    """Bring an attached layer to a new version in place, keeping record keys."""
    key_of = "(SELECT record_key FROM records WHERE id = ?)"
    current = {row["id"]: row for row in db.execute(
        "SELECT id, kind, text, attributes, author_id, created_by, updated_at FROM records WHERE layer_key = ?",
        (key,))}
    incoming = {row["id"]: row for row in snapshot.records}
    changed = [row["id"] for row in snapshot.records
               if row["id"] in current and tuple(current[row["id"]]) != tuple(row)]
    db.executemany(
        """UPDATE records SET kind = ?, text = ?, attributes = ?, author_id = ?, created_by = ?, updated_at = ?
           WHERE id = ?""", ((row["kind"], row["text"], row["attributes"], row["author_id"], row["created_by"],
                             row["updated_at"], row["id"]) for row in snapshot.records if row["id"] in changed))
    db.executemany(
        f"""INSERT INTO records (id, kind, text, attributes, author_id, created_by, updated_at, layer_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, {key})""", (tuple(row) for row in snapshot.records
                                                    if row["id"] not in current))
    # Links: replace by ID; endpoints resolve against the whole workspace.
    old_links = {row["id"]: row for row in db.execute(
        """SELECT l.id, f.id AS from_id, t.id AS to_id, l.relation, l.explanation, l.author_id, l.created_by,
                  l.updated_at FROM links l JOIN records f ON f.record_key = l.from_key
           JOIN records t ON t.record_key = l.to_key WHERE l.layer_key = ?""", (key,))}
    new_links = {row["id"]: tuple(row)[:8] for row in snapshot.links}
    db.executemany("DELETE FROM links WHERE id = ?", ((i,) for i in old_links if i not in new_links))
    links_updated = [i for i, row in new_links.items() if i in old_links and tuple(old_links[i]) != row]
    db.executemany(
        f"""UPDATE links SET from_key = {key_of}, to_key = {key_of}, relation = ?, explanation = ?, author_id = ?,
            created_by = ?, updated_at = ? WHERE id = ?""",
        ((*new_links[i][1:], i) for i in links_updated))
    db.executemany(
        f"""INSERT INTO links (id, from_key, to_key, relation, explanation, author_id, created_by,
            updated_at, layer_key) VALUES (?, {key_of}, {key_of}, ?, ?, ?, ?, ?, {key})""",
        (row for i, row in new_links.items() if i not in old_links))
    # Records dropped upstream go unless something in the workspace still points at them.
    dropped = [i for i in current if i not in incoming]
    kept = []
    for record_id in dropped:
        record_key = db.execute("SELECT record_key FROM records WHERE id = ?", (record_id,)).fetchone()[0]
        if db.execute("""SELECT 1 FROM links WHERE from_key = ? OR to_key = ?
                         UNION ALL SELECT 1 FROM citations WHERE source_key = ? LIMIT 1""",
                      (record_key, record_key, record_key)).fetchone():
            kept.append(record_id)
            continue
        _drop_text(db, record_key)
        db.execute("DELETE FROM citations WHERE record_key = ?", (record_key,))
        db.execute("DELETE FROM withheld_sources WHERE source_key = ?", (record_key,))
        db.execute("DELETE FROM records WHERE record_key = ?", (record_key,))
    # Source text is fixed per Source: new text arrives, withdrawn text leaves, changed text is refused.
    had_text = {row["id"]: (row["record_key"], row["text_sha256"]) for row in db.execute(
        """SELECT r.id, r.record_key, s.text_sha256 FROM source_content s JOIN records r ON r.record_key = s.source_key
           WHERE r.layer_key = ?""", (key,))}
    with_text = {row["id"]: row["text_sha256"] for row in snapshot.contents if not row["withheld"]}
    for record_id, (record_key, digest) in had_text.items():
        if record_id in with_text and with_text[record_id] != digest:
            raise ValueError(f"Source {record_id} text differs from the attached copy; source text is fixed")
        if record_id not in with_text:
            _drop_text(db, record_key)
    db.execute(f"DELETE FROM withheld_sources WHERE source_key IN (SELECT record_key FROM records WHERE layer_key = {key})")
    db.executemany(f"INSERT INTO source_content (source_key, text_utf8, text_sha256) VALUES ({key_of}, ?, ?)",
                   ((row["id"], row["text_utf8"], row["text_sha256"]) for row in snapshot.contents
                    if not row["withheld"] and row["id"] not in had_text))
    db.executemany(f"INSERT INTO withheld_sources (source_key, text_sha256) VALUES ({key_of}, ?)",
                   ((row["id"], row["text_sha256"]) for row in snapshot.contents if row["withheld"]))
    # Citations follow current attributes and available text; the workspace's own are re-derived by the caller.
    db.execute(f"""UPDATE records SET attributes = attributes
                   WHERE layer_key = {key} AND json_type(attributes, '$.citations') = 'array'""")
    db.execute(f"""DELETE FROM passages WHERE source_key IN (SELECT record_key FROM records WHERE layer_key = {key})""")
    db.executemany(f"INSERT INTO passages (source_key, ordinal, start_byte, end_byte) VALUES ({key_of}, ?, ?, ?)",
                   (tuple(row) for row in snapshot.passages))
    db.execute(f"""DELETE FROM document_context WHERE source_key IN
                   (SELECT record_key FROM records WHERE layer_key = {key})""")
    db.executemany(
        f"""INSERT INTO document_context (source_key, title, summary, author_id, updated_at, provenance)
            VALUES ({key_of}, ?, ?, ?, ?, ?)""", (tuple(row) for row in snapshot.contexts))
    # Review: the workspace's own links whose endpoint in this layer changed, and records kept after removal.
    review = {}
    if changed:
        linked = [row[0] for row in db.execute(
            f"""SELECT DISTINCT r.id FROM records r JOIN links l ON r.record_key IN (l.from_key, l.to_key)
                WHERE l.layer_key = {own_key} AND r.id IN (SELECT value FROM json_each(?)) ORDER BY r.id""",
            (json.dumps(changed),))]
        if linked:
            review["changed_linked_records"] = linked
    if kept:
        review["removed_upstream_but_linked"] = sorted(kept)
    return ({"added": len(incoming) - (len(current) - len(dropped)), "updated": len(changed), "removed": len(dropped) - len(kept),
             "kept": len(kept)},
            {"added": len([i for i in new_links if i not in old_links]), "updated": len(links_updated),
             "removed": len([i for i in old_links if i not in new_links])}, review)


def _drop_text(db: sqlite3.Connection, source_key: int) -> None:
    """Remove a Source's text and everything derived from it, in dependency order."""
    for table, column in (("citations", "source_key"), ("document_context", "source_key"),
                          ("passages", "source_key"), ("source_content", "source_key")):
        db.execute(f"DELETE FROM {table} WHERE {column} = ?", (source_key,))
