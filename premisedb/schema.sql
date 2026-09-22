BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS records (
    record_key INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN
        ('Entity', 'Source', 'Finding', 'Question', 'Model', 'Variable')),
    current_revision INTEGER NOT NULL CHECK (current_revision > 0),
    FOREIGN KEY (record_key, current_revision)
        REFERENCES record_revisions (record_key, revision)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS record_revisions (
    record_key INTEGER NOT NULL REFERENCES records (record_key),
    revision INTEGER NOT NULL CHECK (revision > 0),
    text TEXT NOT NULL,
    attributes TEXT NOT NULL CHECK (json_valid(attributes)
        AND json_type(attributes) = 'object'),
    author_id TEXT NOT NULL CHECK (length(trim(author_id)) > 0),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (record_key, revision)
);

CREATE TRIGGER IF NOT EXISTS revisions_no_update
BEFORE UPDATE ON record_revisions BEGIN
    SELECT RAISE(ABORT, 'record revisions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS revisions_no_delete
BEFORE DELETE ON record_revisions BEGIN
    SELECT RAISE(ABORT, 'record revisions are immutable');
END;

PRAGMA user_version = 1;
COMMIT;
