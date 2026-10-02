-- The answer UI's own store: conversation threads, kept apart from the research graph.
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    harness TEXT NOT NULL,
    session_id TEXT,  -- The harness's own session, resumed for follow-up questions.
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE turns (
    id INTEGER PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(id),
    question TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'done', 'failed')),
    progress TEXT NOT NULL DEFAULT '[]',  -- JSON list of what the agent has done so far.
    answer TEXT,  -- The checked answer document as JSON, when done.
    error TEXT,
    written TEXT,  -- JSON object: records the agent added or edited during the turn, by kind.
    denied TEXT,  -- JSON list: tools the harness refused under the workspace's permission settings.
    started_at TEXT NOT NULL,
    finished_at TEXT
) STRICT;

CREATE INDEX turns_by_thread ON turns(thread_id, id);
