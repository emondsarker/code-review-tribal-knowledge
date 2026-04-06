CREATE TABLE IF NOT EXISTS fetch_state (
    repo            TEXT PRIMARY KEY,
    last_fetched_at TEXT NOT NULL,
    last_pr_number  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pull_requests (
    id              INTEGER PRIMARY KEY,
    repo            TEXT NOT NULL,
    number          INTEGER NOT NULL,
    title           TEXT NOT NULL,
    user_login      TEXT NOT NULL,
    merged_at       TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    html_url        TEXT NOT NULL,
    UNIQUE(repo, number)
);

CREATE TABLE IF NOT EXISTS reviews (
    id              INTEGER PRIMARY KEY,
    repo            TEXT NOT NULL,
    pr_number       INTEGER NOT NULL,
    user_login      TEXT NOT NULL,
    state           TEXT NOT NULL,
    body            TEXT,
    submitted_at    TEXT NOT NULL,
    FOREIGN KEY (repo, pr_number) REFERENCES pull_requests(repo, number)
);

CREATE TABLE IF NOT EXISTS comments (
    id                      INTEGER PRIMARY KEY,
    repo                    TEXT NOT NULL,
    pr_number               INTEGER NOT NULL,
    review_id               INTEGER,
    user_login              TEXT NOT NULL,
    body                    TEXT NOT NULL,
    path                    TEXT,
    line                    INTEGER,
    original_line           INTEGER,
    side                    TEXT,
    diff_hunk               TEXT,
    commit_id               TEXT,
    author_association      TEXT,
    in_reply_to_id          INTEGER,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    html_url                TEXT NOT NULL,
    FOREIGN KEY (repo, pr_number) REFERENCES pull_requests(repo, number)
);

CREATE VIRTUAL TABLE IF NOT EXISTS comments_fts USING fts5(
    body,
    path,
    diff_hunk,
    content='comments',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS comments_ai AFTER INSERT ON comments BEGIN
    INSERT INTO comments_fts(rowid, body, path, diff_hunk)
    VALUES (new.id, new.body, new.path, new.diff_hunk);
END;

CREATE TRIGGER IF NOT EXISTS comments_ad AFTER DELETE ON comments BEGIN
    INSERT INTO comments_fts(comments_fts, rowid, body, path, diff_hunk)
    VALUES ('delete', old.id, old.body, old.path, old.diff_hunk);
END;

CREATE TRIGGER IF NOT EXISTS comments_au AFTER UPDATE ON comments BEGIN
    INSERT INTO comments_fts(comments_fts, rowid, body, path, diff_hunk)
    VALUES ('delete', old.id, old.body, old.path, old.diff_hunk);
    INSERT INTO comments_fts(rowid, body, path, diff_hunk)
    VALUES (new.id, new.body, new.path, new.diff_hunk);
END;

CREATE TABLE IF NOT EXISTS comment_embeddings (
    comment_id      INTEGER PRIMARY KEY,
    embedding       BLOB NOT NULL,
    model           TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (comment_id) REFERENCES comments(id)
);

CREATE TABLE IF NOT EXISTS tags (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT UNIQUE NOT NULL,
    description     TEXT
);

CREATE TABLE IF NOT EXISTS comment_tags (
    comment_id      INTEGER NOT NULL,
    tag_id          INTEGER NOT NULL,
    confidence      REAL DEFAULT 1.0,
    PRIMARY KEY (comment_id, tag_id),
    FOREIGN KEY (comment_id) REFERENCES comments(id),
    FOREIGN KEY (tag_id) REFERENCES tags(id)
);

CREATE INDEX IF NOT EXISTS idx_comment_tags_tag ON comment_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_comments_repo_pr ON comments(repo, pr_number);
CREATE INDEX IF NOT EXISTS idx_comments_path ON comments(path);
CREATE INDEX IF NOT EXISTS idx_comments_user ON comments(user_login);
CREATE INDEX IF NOT EXISTS idx_pr_repo_number ON pull_requests(repo, number);
CREATE INDEX IF NOT EXISTS idx_pr_merged_at ON pull_requests(merged_at);

CREATE TABLE IF NOT EXISTS conventions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    category        TEXT,
    file_patterns   TEXT,
    source_comment_ids TEXT NOT NULL,
    confidence      REAL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
