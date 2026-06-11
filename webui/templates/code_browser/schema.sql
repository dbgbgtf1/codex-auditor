PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY,
  lang TEXT NOT NULL,
  sha1 TEXT NOT NULL,
  size INTEGER NOT NULL,
  mtime REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS symbols (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  line INTEGER NOT NULL,
  container TEXT,
  signature TEXT
);

CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(path);

CREATE TABLE IF NOT EXISTS refs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  path TEXT NOT NULL,
  line INTEGER NOT NULL,
  context TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_refs_name ON refs(name);
CREATE INDEX IF NOT EXISTS idx_refs_path ON refs(path);
CREATE UNIQUE INDEX IF NOT EXISTS idx_refs_unique ON refs(name, path, line);

CREATE TABLE IF NOT EXISTS tests (
  path TEXT PRIMARY KEY,
  tags TEXT NOT NULL,
  flags TEXT NOT NULL,
  source_hint TEXT
);

CREATE TABLE IF NOT EXISTS commits (
  hash TEXT PRIMARY KEY,
  subject TEXT NOT NULL,
  date TEXT,
  files TEXT NOT NULL,
  source_files TEXT NOT NULL DEFAULT '[]',
  test_files TEXT NOT NULL DEFAULT '[]',
  diff_hints TEXT NOT NULL DEFAULT '[]',
  audit_signal TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_commits_subject ON commits(subject);

CREATE TABLE IF NOT EXISTS routes (
  pattern TEXT PRIMARY KEY,
  skill TEXT NOT NULL,
  reason TEXT NOT NULL
);

