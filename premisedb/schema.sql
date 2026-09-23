CREATE TABLE records (
    record_key INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN
        ('Entity', 'Source', 'Finding', 'Question', 'Model', 'Variable')),
    text TEXT NOT NULL,
    attributes TEXT NOT NULL CHECK (json_valid(attributes)
        AND json_type(attributes) = 'object'),
    author_id TEXT NOT NULL CHECK (length(trim(author_id)) > 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE links (
    link_key INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    from_key INTEGER NOT NULL REFERENCES records (record_key),
    to_key INTEGER NOT NULL REFERENCES records (record_key),
    relation TEXT NOT NULL CHECK (length(trim(relation)) > 0),
    explanation TEXT NOT NULL,
    author_id TEXT NOT NULL CHECK (length(trim(author_id)) > 0),
    updated_at TEXT NOT NULL
);

CREATE INDEX links_out ON links (from_key, relation, link_key);
CREATE INDEX links_out_all ON links (from_key, link_key);
CREATE INDEX links_in ON links (to_key, relation, link_key);
CREATE INDEX links_in_all ON links (to_key, link_key);

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
CREATE TRIGGER source_content_kind BEFORE INSERT ON source_content
WHEN (SELECT kind FROM records WHERE record_key = new.source_key) != 'Source'
BEGIN
    SELECT RAISE(ABORT, 'source content requires a Source record');
END;
CREATE TRIGGER source_content_fixed BEFORE UPDATE ON source_content BEGIN
    SELECT RAISE(ABORT, 'source text is fixed; ingest a new Source');
END;

CREATE TABLE document_context (
    source_key INTEGER PRIMARY KEY REFERENCES source_content (source_key),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    summary TEXT NOT NULL,
    author_id TEXT NOT NULL CHECK (length(trim(author_id)) > 0),
    updated_at TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK (json_valid(provenance) AND json_type(provenance) = 'object')
);

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
