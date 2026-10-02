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
    effort INTEGER NOT NULL,  -- Minutes the user gave the turn.
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

-- The pieces of work in a research run: a queue for its workers, not knowledge. What a worker learns
-- goes into the graph; `result` is only its summary for the report.
CREATE TABLE work_items (
    id INTEGER PRIMARY KEY,
    turn_id INTEGER NOT NULL REFERENCES turns(id),
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'todo' CHECK (status IN ('todo', 'running', 'done', 'failed')),
    result TEXT,
    started_at TEXT,
    finished_at TEXT
) STRICT;

CREATE INDEX work_items_by_turn ON work_items(turn_id, id);
