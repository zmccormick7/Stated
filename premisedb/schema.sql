-- Every record and link belongs to one layer. A workspace writes only to its own layer; the
-- others hold read-only copies of graphs published elsewhere. Links may cross layers.
CREATE TABLE layers (
    layer_key INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
    version TEXT NOT NULL,  -- Empty for the workspace's own layer.
    writable INTEGER NOT NULL CHECK (writable IN (0, 1)),
    -- The manifest of a published layer: set on the sealed layer in an exported file and kept when
    -- that file is attached.
    content_sha256 TEXT CHECK (content_sha256 IS NULL OR length(content_sha256) = 64),
    exported_at TEXT
);
CREATE UNIQUE INDEX layers_one_writable ON layers (writable) WHERE writable = 1;

CREATE TABLE records (
    record_key INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN
        ('Entity', 'Source', 'Finding', 'Question', 'Model', 'Variable')),
    text TEXT NOT NULL,
    attributes TEXT NOT NULL CHECK (json_valid(attributes)
        AND json_type(attributes) = 'object'),
    author_id TEXT NOT NULL CHECK (length(trim(author_id)) > 0),  -- Last editor.
    created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),  -- Set once on insert.
    updated_at TEXT NOT NULL,
    -- The date the contents are as of: the cutoff the writer worked at, or the date of the write.
    as_of TEXT NOT NULL CHECK (as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    layer_key INTEGER NOT NULL REFERENCES layers (layer_key)
);
CREATE INDEX records_layer ON records (layer_key, record_key);

CREATE TABLE links (
    link_key INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    from_key INTEGER NOT NULL REFERENCES records (record_key),
    to_key INTEGER NOT NULL REFERENCES records (record_key),
    relation TEXT NOT NULL CHECK (length(trim(relation)) > 0),
    explanation TEXT NOT NULL,
    author_id TEXT NOT NULL CHECK (length(trim(author_id)) > 0),  -- Last editor.
    created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),  -- Set once on insert.
    updated_at TEXT NOT NULL,
    as_of TEXT NOT NULL CHECK (as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    layer_key INTEGER NOT NULL REFERENCES layers (layer_key)
);

CREATE INDEX links_out ON links (from_key, relation, link_key);
CREATE INDEX links_out_all ON links (from_key, link_key);
CREATE INDEX links_in ON links (to_key, relation, link_key);
CREATE INDEX links_in_all ON links (to_key, link_key);
CREATE INDEX links_layer ON links (layer_key, link_key);

-- When a read at a cutoff date first sees each record. A Source is seen from its publication date
-- (attributes.published, a date; a Source without one is never seen). A disclosed Finding is seen from
-- the latest publication date of the Sources it cites, since its figure is checked against them
-- whenever it was written; when it cites none, or one without a date, from its stamp. Every other
-- record is seen from its stamp. Entities are reference data and always seen. The views filter
-- evidence by date; they do not reconstruct earlier contents, which are not kept.
CREATE VIEW record_visibility AS
    SELECT r.record_key,
           CASE r.kind
               WHEN 'Entity' THEN ''
               WHEN 'Source' THEN CASE WHEN json_extract(r.attributes, '$.published')
                                            GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
                                       THEN substr(json_extract(r.attributes, '$.published'), 1, 10) END
               WHEN 'Finding' THEN coalesce(
                   CASE WHEN json_extract(r.attributes, '$.basis') = 'disclosed' THEN
                       (SELECT CASE WHEN count(*) > 0 AND count(*) = count(d.published) THEN max(d.published) END
                        FROM (SELECT CASE WHEN json_extract(s.attributes, '$.published')
                                               GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
                                          THEN substr(json_extract(s.attributes, '$.published'), 1, 10) END AS published
                              FROM links l JOIN records s ON s.record_key = l.to_key
                              WHERE l.from_key = r.record_key AND l.relation = 'supported_by'
                                AND s.kind = 'Source') d)
                   END, r.as_of)
               ELSE r.as_of
           END AS visible_from
    FROM records r;
-- A link is seen once its stamp and both its ends are.
CREATE VIEW link_visibility AS
    SELECT l.link_key, max(l.as_of, f.visible_from, t.visible_from) AS visible_from
    FROM links l JOIN record_visibility f ON f.record_key = l.from_key
    JOIN record_visibility t ON t.record_key = l.to_key;

-- Read-only layers change only when a new version of them is attached, never through edits.
CREATE TRIGGER records_read_only_insert BEFORE INSERT ON records
WHEN (SELECT writable FROM layers WHERE layer_key = new.layer_key) = 0 BEGIN
    SELECT RAISE(ABORT, 'records can only be added to the writable layer');
END;
CREATE TRIGGER records_read_only_update BEFORE UPDATE ON records
WHEN new.layer_key != old.layer_key
  OR (SELECT writable FROM layers WHERE layer_key = old.layer_key) = 0 BEGIN
    SELECT RAISE(ABORT, 'the record belongs to a read-only layer; add your own record and link it');
END;
CREATE TRIGGER records_read_only_delete BEFORE DELETE ON records
WHEN (SELECT writable FROM layers WHERE layer_key = old.layer_key) = 0 BEGIN
    SELECT RAISE(ABORT, 'the record belongs to a read-only layer');
END;
CREATE TRIGGER links_read_only_insert BEFORE INSERT ON links
WHEN (SELECT writable FROM layers WHERE layer_key = new.layer_key) = 0 BEGIN
    SELECT RAISE(ABORT, 'links can only be added to the writable layer');
END;
CREATE TRIGGER links_read_only_update BEFORE UPDATE ON links
WHEN new.layer_key != old.layer_key
  OR (SELECT writable FROM layers WHERE layer_key = old.layer_key) = 0 BEGIN
    SELECT RAISE(ABORT, 'the link belongs to a read-only layer; add your own link instead');
END;
CREATE TRIGGER links_read_only_delete BEFORE DELETE ON links
WHEN (SELECT writable FROM layers WHERE layer_key = old.layer_key) = 0 BEGIN
    SELECT RAISE(ABORT, 'the link belongs to a read-only layer');
END;

CREATE VIRTUAL TABLE records_fts USING fts5(
    text, content='records', content_rowid='record_key'
);
CREATE TRIGGER records_fts_insert AFTER INSERT ON records BEGIN
    INSERT INTO records_fts(rowid, text) VALUES (new.record_key, new.text);
END;
CREATE TRIGGER records_fts_update AFTER UPDATE ON records BEGIN
    INSERT INTO records_fts(records_fts, rowid, text)
        VALUES ('delete', old.record_key, old.text);
    INSERT INTO records_fts(rowid, text) VALUES (new.record_key, new.text);
END;
CREATE TRIGGER records_fts_delete AFTER DELETE ON records BEGIN
    INSERT INTO records_fts(records_fts, rowid, text)
        VALUES ('delete', old.record_key, old.text);
END;

CREATE TABLE source_content (
    source_key INTEGER PRIMARY KEY REFERENCES records (record_key),
    text_utf8 BLOB NOT NULL CHECK (typeof(text_utf8) = 'blob' AND length(text_utf8) > 0),
    text_sha256 TEXT NOT NULL CHECK (length(text_sha256) = 64)
);
-- Finds an already-ingested copy of the same text without reading document bodies.
CREATE INDEX source_content_sha256 ON source_content (text_sha256);
CREATE TRIGGER source_content_kind BEFORE INSERT ON source_content
WHEN (SELECT kind FROM records WHERE record_key = new.source_key) != 'Source'
BEGIN
    SELECT RAISE(ABORT, 'source content requires a Source record');
END;
CREATE TRIGGER source_content_fixed BEFORE UPDATE ON source_content BEGIN
    SELECT RAISE(ABORT, 'source text is fixed; ingest a new Source');
END;

-- Sources whose text a published layer withholds (Source attributes.redistributable is false). The
-- hash lets a workspace that holds the document verify and supply the text itself.
CREATE TABLE withheld_sources (
    source_key INTEGER PRIMARY KEY REFERENCES records (record_key),
    text_sha256 TEXT NOT NULL CHECK (length(text_sha256) = 64)
);

CREATE TABLE document_context (
    source_key INTEGER PRIMARY KEY REFERENCES source_content (source_key),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    summary TEXT NOT NULL,
    author_id TEXT NOT NULL CHECK (length(trim(author_id)) > 0),
    updated_at TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK (json_valid(provenance) AND json_type(provenance) = 'object')
);
-- Context is part of its Source's layer.
CREATE TRIGGER document_context_read_only BEFORE INSERT ON document_context
WHEN (SELECT y.writable FROM records r JOIN layers y ON y.layer_key = r.layer_key
      WHERE r.record_key = new.source_key) = 0 BEGIN
    SELECT RAISE(ABORT, 'the Source belongs to a read-only layer');
END;

CREATE TABLE passages (
    passage_key INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key INTEGER NOT NULL REFERENCES source_content (source_key),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    start_byte INTEGER NOT NULL CHECK (start_byte >= 0),
    end_byte INTEGER NOT NULL CHECK (end_byte > start_byte),
    UNIQUE (source_key, ordinal)
);

-- Index terms without storing another copy of the document text.
CREATE VIEW passage_search_content AS
    SELECT p.passage_key, p.source_key,
           coalesce(c.title || char(10) || c.summary, '') AS header,
           CAST(substr(s.text_utf8, p.start_byte + 1, p.end_byte - p.start_byte) AS TEXT) AS text
    FROM passages p JOIN source_content s ON s.source_key = p.source_key
    LEFT JOIN document_context c ON c.source_key = p.source_key;

CREATE VIRTUAL TABLE passages_fts USING fts5(header, text, content='', contentless_delete=1);
CREATE TRIGGER passages_fts_insert AFTER INSERT ON passages BEGIN
    INSERT INTO passages_fts(rowid, header, text)
        SELECT passage_key, header, text FROM passage_search_content WHERE passage_key = new.passage_key;
END;
CREATE TRIGGER passages_fts_update AFTER UPDATE ON passages BEGIN
    DELETE FROM passages_fts WHERE rowid = old.passage_key;
    INSERT INTO passages_fts(rowid, header, text)
        SELECT passage_key, header, text FROM passage_search_content WHERE passage_key = new.passage_key;
END;
CREATE TRIGGER passages_fts_delete AFTER DELETE ON passages BEGIN
    DELETE FROM passages_fts WHERE rowid = old.passage_key;
END;

CREATE TRIGGER document_context_insert AFTER INSERT ON document_context BEGIN
    DELETE FROM passages_fts WHERE rowid IN
        (SELECT passage_key FROM passages WHERE source_key = new.source_key);
    INSERT INTO passages_fts(rowid, header, text)
        SELECT passage_key, header, text FROM passage_search_content WHERE source_key = new.source_key;
END;
CREATE TRIGGER document_context_update AFTER UPDATE ON document_context BEGIN
    DELETE FROM passages_fts WHERE rowid IN
        (SELECT passage_key FROM passages WHERE source_key IN (old.source_key, new.source_key));
    INSERT INTO passages_fts(rowid, header, text)
        SELECT passage_key, header, text FROM passage_search_content
        WHERE source_key IN (old.source_key, new.source_key);
END;
CREATE TRIGGER document_context_delete AFTER DELETE ON document_context BEGIN
    DELETE FROM passages_fts WHERE rowid IN
        (SELECT passage_key FROM passages WHERE source_key = old.source_key);
    INSERT INTO passages_fts(rowid, header, text)
        SELECT passage_key, header, text FROM passage_search_content WHERE source_key = old.source_key;
END;

-- Reverse citations: which records cite which source ranges. Derived from each record's
-- attributes.citations and rewritten in the same transaction as every record write.
-- Entries that do not name an ingested Source and a valid byte range are not indexed.
CREATE TABLE citations (
    record_key INTEGER NOT NULL REFERENCES records (record_key),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    source_key INTEGER NOT NULL REFERENCES source_content (source_key),
    start_byte INTEGER NOT NULL CHECK (start_byte >= 0),
    end_byte INTEGER NOT NULL CHECK (end_byte > start_byte),
    PRIMARY KEY (record_key, ordinal)
);
CREATE INDEX citations_by_source ON citations (source_key, start_byte);

CREATE VIEW record_citations AS
    -- Read fields by path from the whole document: non-object entries yield NULL, not errors.
    SELECT r.record_key, c.key AS ordinal, s.source_key,
           json_extract(r.attributes, c.fullkey || '.start_byte') AS start_byte,
           json_extract(r.attributes, c.fullkey || '.end_byte') AS end_byte
    FROM records r JOIN json_each(r.attributes, '$.citations') c
    JOIN records cited ON cited.id = json_extract(r.attributes, c.fullkey || '.source_id')
    JOIN source_content s ON s.source_key = cited.record_key
    WHERE json_type(r.attributes, '$.citations') = 'array' AND c.type = 'object'
      AND json_type(r.attributes, c.fullkey || '.start_byte') = 'integer'
      AND json_type(r.attributes, c.fullkey || '.end_byte') = 'integer'
      AND json_extract(r.attributes, c.fullkey || '.start_byte') >= 0
      AND json_extract(r.attributes, c.fullkey || '.end_byte')
          > json_extract(r.attributes, c.fullkey || '.start_byte')
      AND json_extract(r.attributes, c.fullkey || '.end_byte') <= length(s.text_utf8);

CREATE TRIGGER citations_insert AFTER INSERT ON records BEGIN
    INSERT INTO citations SELECT * FROM record_citations WHERE record_key = new.record_key;
END;
CREATE TRIGGER citations_update AFTER UPDATE OF attributes ON records BEGIN
    DELETE FROM citations WHERE record_key = old.record_key;
    INSERT INTO citations SELECT * FROM record_citations WHERE record_key = new.record_key;
END;
