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
