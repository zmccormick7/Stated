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
