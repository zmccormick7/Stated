BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS links (
    link_key INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    current_revision INTEGER NOT NULL CHECK (current_revision > 0),
    from_key INTEGER NOT NULL REFERENCES records (record_key),
    to_key INTEGER NOT NULL REFERENCES records (record_key),
    relation TEXT NOT NULL CHECK (length(trim(relation)) > 0),
    FOREIGN KEY (link_key, current_revision)
        REFERENCES link_revisions (link_key, revision) DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX IF NOT EXISTS links_out ON links (from_key, relation, link_key);
CREATE INDEX IF NOT EXISTS links_in ON links (to_key, relation, link_key);
CREATE INDEX IF NOT EXISTS links_out_all ON links (from_key, link_key);
CREATE INDEX IF NOT EXISTS links_in_all ON links (to_key, link_key);

CREATE TABLE IF NOT EXISTS link_revisions (
    link_key INTEGER NOT NULL REFERENCES links (link_key),
    revision INTEGER NOT NULL CHECK (revision > 0),
    from_key INTEGER NOT NULL REFERENCES records (record_key),
    to_key INTEGER NOT NULL REFERENCES records (record_key),
    relation TEXT NOT NULL CHECK (length(trim(relation)) > 0),
    from_revision INTEGER CHECK (from_revision > 0),
    to_revision INTEGER CHECK (to_revision > 0),
    explanation TEXT NOT NULL,
    author_id TEXT NOT NULL CHECK (length(trim(author_id)) > 0),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (link_key, revision),
    FOREIGN KEY (from_key, from_revision) REFERENCES record_revisions (record_key, revision),
    FOREIGN KEY (to_key, to_revision) REFERENCES record_revisions (record_key, revision),
    CHECK (relation != 'supported_by' OR (from_revision IS NOT NULL AND to_revision IS NOT NULL))
);
CREATE TRIGGER IF NOT EXISTS links_no_update
BEFORE UPDATE ON link_revisions BEGIN
    SELECT RAISE(ABORT, 'link revisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS links_no_delete
BEFORE DELETE ON link_revisions BEGIN
    SELECT RAISE(ABORT, 'link revisions are immutable');
END;
PRAGMA user_version = 2;
COMMIT;
