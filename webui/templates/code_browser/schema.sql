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
  mtime REAL NOT NULL,
  in_compile_db INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS symbols (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  usr TEXT,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  line INTEGER NOT NULL,
  column INTEGER NOT NULL DEFAULT 0,
  extent_start_line INTEGER,
  extent_start_column INTEGER,
  extent_end_line INTEGER,
  extent_end_column INTEGER,
  is_definition INTEGER NOT NULL DEFAULT 0,
  type TEXT,
  signature TEXT,
  backend TEXT NOT NULL DEFAULT 'libclang'
);

CREATE INDEX IF NOT EXISTS idx_symbols_usr ON symbols(usr);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_path_line ON symbols(path, line);
CREATE UNIQUE INDEX IF NOT EXISTS idx_symbols_unique
  ON symbols(COALESCE(usr, ''), name, kind, path, line, column, is_definition);

CREATE TABLE IF NOT EXISTS refs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  referenced_usr TEXT,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  line INTEGER NOT NULL,
  column INTEGER NOT NULL DEFAULT 0,
  context TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_refs_usr ON refs(referenced_usr);
CREATE INDEX IF NOT EXISTS idx_refs_name ON refs(name);
CREATE INDEX IF NOT EXISTS idx_refs_path_line ON refs(path, line);
CREATE UNIQUE INDEX IF NOT EXISTS idx_refs_unique
  ON refs(COALESCE(referenced_usr, ''), name, kind, path, line, column);

CREATE TABLE IF NOT EXISTS diagnostics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT,
  line INTEGER,
  column INTEGER,
  severity INTEGER NOT NULL,
  message TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_diagnostics_path ON diagnostics(path);
CREATE INDEX IF NOT EXISTS idx_diagnostics_severity ON diagnostics(severity);

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
