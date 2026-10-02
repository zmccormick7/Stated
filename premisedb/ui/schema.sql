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
    agents INTEGER NOT NULL DEFAULT 1,  -- Workers at a time, for a research run.
    paused INTEGER NOT NULL DEFAULT 0,  -- The user asked a run to start nothing new until resumed.
    finish INTEGER NOT NULL DEFAULT 0,  -- The user asked a run to go to its report now.
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
    title TEXT NOT NULL,  -- One line, for the page and the lists agents see.
    text TEXT NOT NULL,  -- What to do, for the worker that takes it.
    status TEXT NOT NULL DEFAULT 'todo' CHECK (status IN ('todo', 'running', 'done', 'failed')),
    result TEXT,
    progress TEXT,  -- The worker's latest step, for the page.
    started_at TEXT,
    finished_at TEXT
) STRICT;

CREATE INDEX work_items_by_turn ON work_items(turn_id, id);

-- What the user told a run while it was going; every later session of the run sees them.
CREATE TABLE notes (
    id INTEGER PRIMARY KEY,
    turn_id INTEGER NOT NULL REFERENCES turns(id),
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX notes_by_turn ON notes(turn_id, id);
