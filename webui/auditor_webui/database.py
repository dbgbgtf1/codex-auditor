"""SQLite persistence for WebUI sessions, messages, and runs."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from .config import CONFIG, IDENTIFIER_RE
from .schema import BUSY_STATUSES, JsonObject, RowDict


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_dirs() -> None:
    CONFIG.audit_dir.mkdir(parents=True, exist_ok=True)
    CONFIG.temp_dir.mkdir(parents=True, exist_ok=True)
    CONFIG.workspace.mkdir(parents=True, exist_ok=True)


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(CONFIG.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    ensure_dirs()
    with connect_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                identifier TEXT NOT NULL,
                codex_session_id TEXT,
                status TEXT NOT NULL DEFAULT 'idle',
                auto_continue INTEGER NOT NULL DEFAULT 1,
                auto_continue_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                last_stop_reason TEXT,
                last_vuln_error TEXT,
                last_vuln_request_at REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'message',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                source TEXT NOT NULL,
                prompt TEXT NOT NULL,
                model TEXT NOT NULL,
                status TEXT NOT NULL,
                returncode INTEGER,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                codex_session_id_before TEXT,
                codex_session_id_after TEXT,
                last_message TEXT,
                judge_result TEXT,
                error TEXT
            );
            """,
        )
        set_default_setting(conn, "theme", "light")
        set_default_setting(conn, "selected_session_id", "")
        conn.execute("UPDATE sessions SET auto_continue = 1")
        conn.execute("UPDATE sessions SET status = 'idle' WHERE status IN ('running', 'judging')")
        conn.execute("UPDATE runs SET status = 'interrupted', ended_at = ? WHERE status = 'running'", (now_iso(),))


def set_default_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
        (key, value, now_iso()),
    )


def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows}


def set_setting(key: str, value: str) -> None:
    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO settings(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, now_iso()),
        )


def row_to_dict(row: sqlite3.Row) -> RowDict:
    result: RowDict = {}
    for key, value in dict(row).items():
        if value is None or isinstance(value, (str, int, float)):
            result[key] = value
        else:
            result[key] = str(value)
    return result


def normalize_identifier(raw: str) -> str:
    identifier = raw.strip().upper().replace("-", "_")
    if not IDENTIFIER_RE.fullmatch(identifier):
        raise ValueError("标识符必须以字母开头，只能包含大写字母、数字和下划线")
    return identifier


def get_session(session_id: int) -> RowDict | None:
    with connect_db() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return row_to_dict(row) if row else None


def get_existing_session(session_id: int) -> RowDict:
    session = get_session(session_id)
    if not session:
        raise KeyError("会话不存在")
    return session


def list_sessions() -> list[RowDict]:
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT id, name, identifier, codex_session_id, status, auto_continue,
                   auto_continue_count, last_error, last_stop_reason, created_at, updated_at
            FROM sessions
            ORDER BY datetime(updated_at) DESC, id DESC
            """,
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def create_session(payload: JsonObject) -> RowDict:
    identifier = normalize_identifier(str(payload.get("identifier", "")))
    name = str(payload.get("name", "")).strip() or f"{identifier} 审计"
    created_at = now_iso()
    with connect_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO sessions(name, identifier, auto_continue, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, identifier, 1, created_at, created_at),
        )
        if cur.lastrowid is None:
            raise RuntimeError("会话创建后没有返回 id")
        session_id = cur.lastrowid
        conn.execute(
            """
            INSERT INTO messages(session_id, role, content, kind, created_at)
            VALUES (?, 'system', ?, 'session', ?)
            """,
            (session_id, f"会话已创建，标识符 {identifier}。", created_at),
        )
        conn.execute(
            """
            INSERT INTO settings(key, value, updated_at)
            VALUES ('selected_session_id', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (str(session_id), created_at),
        )
    session = get_session(session_id)
    if session is None:
        raise RuntimeError("会话创建后无法读取")
    return session


def update_session(session_id: int, payload: JsonObject) -> RowDict:
    current = get_existing_session(session_id)
    fields: list[str] = []
    values: list[object] = []
    if "name" in payload:
        name = str(payload["name"]).strip()
        if not name:
            raise ValueError("会话名称不能为空")
        fields.append("name = ?")
        values.append(name)
    if "identifier" in payload:
        fields.append("identifier = ?")
        values.append(normalize_identifier(str(payload["identifier"])))
    if not fields:
        return current
    fields.append("updated_at = ?")
    values.append(now_iso())
    values.append(session_id)
    with connect_db() as conn:
        conn.execute(f"UPDATE sessions SET {', '.join(fields)} WHERE id = ?", values)
    return get_existing_session(session_id)


def delete_session(session_id: int) -> None:
    current = get_existing_session(session_id)
    if current["status"] in BUSY_STATUSES:
        raise ValueError("会话任务处理中，不能删除")
    with connect_db() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        settings = get_settings(conn)
        if settings.get("selected_session_id") == str(session_id):
            conn.execute(
                """
                INSERT INTO settings(key, value, updated_at)
                VALUES ('selected_session_id', '', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (now_iso(),),
            )


def add_message(session_id: int, role: str, content: str, kind: str = "message") -> None:
    content = content.strip()
    if not content:
        return
    timestamp = now_iso()
    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO messages(session_id, role, content, kind, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, role, content, kind, timestamp),
        )
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (timestamp, session_id))


def list_messages(session_id: int, after_id: int = 0, limit: int = 300) -> list[RowDict]:
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT id, session_id, role, content, kind, created_at
            FROM messages
            WHERE session_id = ? AND id > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (session_id, after_id, limit),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def latest_user_input(session_id: int) -> str:
    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT content
            FROM messages
            WHERE session_id = ? AND role = 'user' AND kind = 'message'
            ORDER BY id DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
    return str(row["content"]).strip() if row else ""


def create_run(
    *,
    session_id: int,
    source: str,
    prompt: str,
    model: str,
    started_at: str,
    codex_session_id_before: str | None,
) -> int:
    with connect_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO runs(session_id, source, prompt, model, status, started_at, codex_session_id_before)
            VALUES (?, ?, ?, ?, 'running', ?, ?)
            """,
            (session_id, source, prompt, model, started_at, codex_session_id_before),
        )
        conn.execute(
            """
            UPDATE sessions
            SET status = 'running', last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (started_at, session_id),
        )
        if cur.lastrowid is None:
            raise RuntimeError("运行记录创建后没有返回 id")
        return cur.lastrowid


def update_session_status(session_id: int, status: str) -> None:
    with connect_db() as conn:
        conn.execute("UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), session_id))


def update_run_result(
    *,
    session_id: int,
    run_id: int,
    status: str,
    returncode: int,
    ended_at: str,
    codex_session_id_after: str | None,
    last_message: str,
    judge_result_json: str | None,
    error: str | None,
    last_error: str | None,
    last_stop_reason: str | None,
) -> None:
    with connect_db() as conn:
        if codex_session_id_after:
            conn.execute(
                "UPDATE sessions SET codex_session_id = ? WHERE id = ?",
                (codex_session_id_after, session_id),
            )
        conn.execute(
            """
            UPDATE runs
            SET status = ?, returncode = ?, ended_at = ?, codex_session_id_after = ?,
                last_message = ?, judge_result = ?, error = ?
            WHERE id = ?
            """,
            (
                status,
                returncode,
                ended_at,
                codex_session_id_after,
                last_message,
                judge_result_json,
                error,
                run_id,
            ),
        )
        conn.execute(
            """
            UPDATE sessions
            SET status = 'idle', last_error = ?, last_stop_reason = ?, updated_at = ?
            WHERE id = ?
            """,
            (last_error, last_stop_reason, ended_at, session_id),
        )


def increment_auto_continue(session_id: int) -> None:
    with connect_db() as conn:
        conn.execute("UPDATE sessions SET auto_continue_count = auto_continue_count + 1 WHERE id = ?", (session_id,))


def reset_auto_continue(session_id: int) -> None:
    with connect_db() as conn:
        conn.execute("UPDATE sessions SET auto_continue_count = 0 WHERE id = ?", (session_id,))
