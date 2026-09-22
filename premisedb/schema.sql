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
