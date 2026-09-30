"""Publish a workspace's own layer as a database file that other workspaces can attach.

A published file is an ordinary PremiseDB database holding one sealed (read-only) layer, so every
command that reads a workspace also reads a published layer. Its `layers` row is the manifest.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path


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

    source = store._db
    source.execute("BEGIN")  # One read snapshot for every table.
    try:
        records = source.execute(
            """SELECT id, kind, text, attributes, author_id, created_by, updated_at FROM records
               WHERE layer_key = ? ORDER BY record_key""", (key,)).fetchall()
        outside = source.execute(
            """SELECT count(*) FROM links l JOIN records f ON f.record_key = l.from_key
               JOIN records t ON t.record_key = l.to_key
               WHERE l.layer_key = ? AND (f.layer_key != ? OR t.layer_key != ?)""", (key, key, key)).fetchone()[0]
        if outside:
            raise ValueError(f"{outside} links reach records in other layers; "
                             "exporting a layer that builds on attached layers is not supported yet")
        links = source.execute(
            """SELECT l.id, f.id AS from_id, t.id AS to_id, l.relation, l.explanation, l.author_id,
                      l.created_by, l.updated_at
               FROM links l JOIN records f ON f.record_key = l.from_key JOIN records t ON t.record_key = l.to_key
               WHERE l.layer_key = ? ORDER BY l.link_key""", (key,)).fetchall()
        contents = source.execute(
            """SELECT r.id, s.text_utf8, s.text_sha256,
                      json_type(r.attributes, '$.redistributable') = 'false' AS withheld
               FROM source_content s JOIN records r ON r.record_key = s.source_key
               WHERE r.layer_key = ? ORDER BY r.record_key""", (key,)).fetchall()
        passages = source.execute(
            """SELECT r.id, p.ordinal, p.start_byte, p.end_byte FROM passages p
               JOIN records r ON r.record_key = p.source_key
               WHERE r.layer_key = ? ORDER BY p.source_key, p.ordinal""", (key,)).fetchall()
        contexts = source.execute(
            """SELECT r.id, c.title, c.summary, c.author_id, c.updated_at, c.provenance FROM document_context c
               JOIN records r ON r.record_key = c.source_key
               WHERE r.layer_key = ? ORDER BY c.source_key""", (key,)).fetchall()
    finally:
        source.execute("COMMIT")

    withheld_ids = {row["id"] for row in contents if row["withheld"]}
    # Passages and context describe text the file does not carry; a workspace supplying the text rebuilds them.
    passages = [row for row in passages if row["id"] not in withheld_ids]
    contexts = [row for row in contexts if row["id"] not in withheld_ids]

    digest = sha256()
    for table in (records, links, passages, contexts):
        for row in table:
            digest.update(json.dumps(list(row), ensure_ascii=False).encode("utf-8") + b"\n")
    for row in contents:  # Text is identified by its hash whether or not the file carries it.
        digest.update(json.dumps([row["id"], row["text_sha256"], bool(row["withheld"])]).encode("utf-8") + b"\n")
    content_sha256 = digest.hexdigest()

    with RecordStore(path, layer=name) as out:
        db = out._db
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany(
                """INSERT INTO records (id, kind, text, attributes, author_id, created_by, updated_at, layer_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1)""", (tuple(row) for row in records))
            key_of = "(SELECT record_key FROM records WHERE id = ?)"
            db.executemany(f"INSERT INTO source_content (source_key, text_utf8, text_sha256) VALUES ({key_of}, ?, ?)",
                           ((row["id"], row["text_utf8"], row["text_sha256"]) for row in contents if not row["withheld"]))
            db.executemany(f"INSERT INTO withheld_sources (source_key, text_sha256) VALUES ({key_of}, ?)",
                           ((row["id"], row["text_sha256"]) for row in contents if row["withheld"]))
            # Citations were indexed before their sources' text existed; the update trigger re-derives them.
            db.execute("UPDATE records SET attributes = attributes WHERE json_type(attributes, '$.citations') = 'array'")
            db.executemany(f"INSERT INTO passages (source_key, ordinal, start_byte, end_byte) VALUES ({key_of}, ?, ?, ?)",
                           (tuple(row) for row in passages))
            db.executemany(
                f"""INSERT INTO document_context (source_key, title, summary, author_id, updated_at, provenance)
                    VALUES ({key_of}, ?, ?, ?, ?, ?)""", (tuple(row) for row in contexts))
            db.executemany(
                f"""INSERT INTO links (id, from_key, to_key, relation, explanation, author_id, created_by,
                    updated_at, layer_key) VALUES (?, {key_of}, {key_of}, ?, ?, ?, ?, ?, 1)""",
                (tuple(row) for row in links))
            db.execute("UPDATE layers SET writable = 0, version = ?, content_sha256 = ?, exported_at = ?",
                       (version, content_sha256, datetime.now(UTC).isoformat()))
    return LayerExport(str(path), name, version, len(records), len(links), len(contents) - len(withheld_ids),
                       len(withheld_ids), content_sha256)
