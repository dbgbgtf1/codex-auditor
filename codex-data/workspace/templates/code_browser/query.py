#!/usr/bin/env python3
import argparse
import json
import re
import sqlite3
import subprocess
from pathlib import Path


DEFAULT_DB = Path(".audit") / "code_browser.sqlite"
LOCATION_RE = re.compile(r"^(?P<path>.+):(?P<start>\d+)(?:-(?P<end>\d+))?$")


def workspace_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def resolve_db(workspace: Path, db: Path) -> Path:
    return db if db.is_absolute() else workspace / db


def connect(db: Path):
    if not db.exists():
        raise SystemExit(f"找不到索引: {db}\n请先运行 code_browser/build_index.py")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def meta_dict(conn) -> dict[str, str]:
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta")}


def git_output(workspace: Path, args: list[str]) -> str | None:
    if not (workspace / ".git").exists():
        return None
    try:
        return subprocess.check_output(
            ["git", "-C", str(workspace), *args],
            text=True,
            errors="replace",
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def current_git_metadata(workspace: Path) -> dict[str, str]:
    head = git_output(workspace, ["rev-parse", "HEAD"]) or ""
    status = git_output(workspace, ["status", "--porcelain"])
    return {
        "current_git_head": head,
        "current_git_dirty": "unknown" if status is None else ("yes" if status else "no"),
        "current_git_status_count": "unknown" if status is None else str(len(status.splitlines())),
    }


def freshness(meta: dict[str, str], current: dict[str, str]) -> str:
    indexed = meta.get("git_head", "")
    current_head = current.get("current_git_head", "")
    if not indexed:
        return "未知：索引缺少 git_head"
    if not current_head:
        return "未知：当前 git head 不可用"
    if indexed != current_head:
        return f"过期：索引 {indexed[:12]} 当前 {current_head[:12]}"
    return "新鲜"


def normalize_path(workspace: Path, value: str) -> str:
    text = str(value).strip()
    if not text:
        return text
    path = Path(text)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(workspace).as_posix()
        except ValueError:
            return path.as_posix()
    if text.startswith("./"):
        text = text[2:]
    return text


def split_location(workspace: Path, value: str):
    text = str(value).strip()
    match = LOCATION_RE.match(text)
    if not match:
        return normalize_path(workspace, text), None, None
    path = normalize_path(workspace, match.group("path"))
    start = int(match.group("start"))
    end = int(match.group("end") or start)
    if end < start:
        raise SystemExit(f"无效行号范围: {start}-{end}")
    return path, start, end


def print_rows(rows, fields):
    for row in rows:
        print(" | ".join(str(row[field] if row[field] is not None else "") for field in fields))


def json_list(value) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in data] if isinstance(data, list) else []


def indexed_paths(conn) -> list[str]:
    meta = meta_dict(conn)
    try:
        paths = json.loads(meta.get("indexed_paths", "[]"))
    except json.JSONDecodeError:
        paths = []
    return [str(path) for path in paths] or ["."]


def source_window(workspace: Path, path: str, start: int, end: int):
    abs_path = workspace / path
    if not abs_path.exists():
        print(f"缺少文件: {path}")
        return
    lines = abs_path.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, start)
    end = min(len(lines), end)
    for line_no in range(start, end + 1):
        print(f"{path}:{line_no}: {lines[line_no - 1]}")


def find_pattern_line(workspace: Path, path: str, pattern: str):
    regex = re.compile(pattern)
    abs_path = workspace / path
    if not abs_path.exists():
        return None
    for line_no, line in enumerate(abs_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if regex.search(line):
            return line_no
    return None


def fetch_files(conn, workspace: Path, term: str, limit: int | None):
    path, start, end = split_location(workspace, term)
    if start:
        return path, start, end, []
    like = f"%{path}%"
    sql = """
        SELECT path, lang, size FROM files
        WHERE path = ? OR path LIKE ?
        ORDER BY CASE WHEN path = ? THEN 0 ELSE 1 END, path
    """
    params: list[object] = [path, like, path]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return None, None, None, conn.execute(sql, params).fetchall()


def fetch_symbols(conn, name: str, limit: int):
    like = f"%{name}%"
    return conn.execute(
        """
        SELECT name, kind, path, line, COALESCE(container, '') AS container, COALESCE(signature, '') AS signature
        FROM symbols
        WHERE name = ? OR name LIKE ?
        ORDER BY CASE WHEN name = ? THEN 0 ELSE 1 END, path, line
        LIMIT ?
        """,
        (name, like, name, limit),
    ).fetchall()


def route_for(conn, path: str):
    normalized = path.strip("/")
    routes = conn.execute("SELECT pattern, skill, reason FROM routes ORDER BY LENGTH(pattern) DESC").fetchall()
    for row in routes:
        if row["pattern"].strip("/") in normalized:
            return row
    return {"pattern": "<none>", "skill": "target-audit-index", "reason": "没有匹配路由；请使用目标 audit-index 或创建 route"}


def live_rg_refs(conn, workspace: Path, name: str, limit: int):
    paths = indexed_paths(conn)
    cmd = ["rg", "--line-number", "--fixed-strings", "--glob", "!build/**", "--glob", "!out/**", "--", name, *paths]
    rows = []
    try:
        proc = subprocess.Popen(cmd, cwd=workspace, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return rows
    assert proc.stdout is not None
    for line in proc.stdout:
        if len(rows) >= limit:
            proc.terminate()
            break
        parts = line.rstrip("\n").split(":", 2)
        if len(parts) != 3:
            continue
        path, line_text, context = parts
        try:
            line_no = int(line_text)
        except ValueError:
            continue
        rows.append({"name": name, "path": path, "line": line_no, "context": context.strip()[:300]})
    proc.wait()
    return rows


def cmd_meta(args):
    workspace = workspace_path(args.workspace)
    conn = connect(resolve_db(workspace, args.db))
    meta = meta_dict(conn)
    current = current_git_metadata(workspace)
    for key in sorted(meta):
        print(f"{key}: {meta[key]}")
    for key in sorted(current):
        print(f"{key}: {current[key]}")
    print(f"index_freshness: {freshness(meta, current)}")


def cmd_file(args):
    workspace = workspace_path(args.workspace)
    conn = connect(resolve_db(workspace, args.db))
    path, start, end, rows = fetch_files(conn, workspace, args.term or "", args.limit)
    if path and start:
        source_window(workspace, path, start, end)
        return
    if args.pattern:
        regex = re.compile(args.pattern)
        filtered = []
        for row in rows:
            try:
                text = (workspace / row["path"]).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if regex.search(text):
                filtered.append(row)
        rows = filtered[: args.limit] if args.limit else filtered
    print_rows(rows, ["path", "lang", "size"])


def cmd_symbol(args):
    workspace = workspace_path(args.workspace)
    conn = connect(resolve_db(workspace, args.db))
    print_rows(fetch_symbols(conn, args.name, args.limit), ["name", "kind", "path", "line", "container", "signature"])


def cmd_refs(args):
    workspace = workspace_path(args.workspace)
    conn = connect(resolve_db(workspace, args.db))
    rows = conn.execute(
        """
        SELECT name, path, line, context FROM refs
        WHERE name = ?
        ORDER BY path, line
        LIMIT ?
        """,
        (args.name, args.limit),
    ).fetchall()
    out = [dict(row) for row in rows]
    if len(out) < args.limit and not args.no_rg:
        out.extend(live_rg_refs(conn, workspace, args.name, args.limit - len(out)))
    for row in out[: args.limit]:
        print(f"{row['path']}:{row['line']}: {row['context']}")


def cmd_tests(args):
    workspace = workspace_path(args.workspace)
    conn = connect(resolve_db(workspace, args.db))
    term = args.term or ""
    like = f"%{term}%"
    rows = conn.execute(
        """
        SELECT path, tags, flags, COALESCE(source_hint, '') AS source_hint
        FROM tests
        WHERE path LIKE ? OR tags LIKE ? OR flags LIKE ? OR source_hint LIKE ?
        ORDER BY path
        LIMIT ?
        """,
        (like, like, like, like, args.limit),
    ).fetchall()
    for row in rows:
        print(f"{row['path']} | tags={','.join(json_list(row['tags'])) or '-'} | flags={','.join(json_list(row['flags'])) or '-'} | hint={row['source_hint'] or '-'}")


def cmd_commits(args):
    workspace = workspace_path(args.workspace)
    conn = connect(resolve_db(workspace, args.db))
    term = args.term or ""
    like = f"%{term}%"
    rows = conn.execute(
        """
        SELECT hash, date, subject, source_files, test_files, diff_hints, audit_signal
        FROM commits
        WHERE subject LIKE ? OR files LIKE ? OR source_files LIKE ? OR test_files LIKE ? OR diff_hints LIKE ? OR audit_signal LIKE ?
        ORDER BY date DESC
        LIMIT ?
        """,
        (like, like, like, like, like, like, args.limit),
    ).fetchall()
    for row in rows:
        print(
            f"{row['hash'][:12]} | {row['date']} | {row['subject']} | "
            f"src={','.join(json_list(row['source_files'])[:3]) or '-'} | "
            f"test={','.join(json_list(row['test_files'])[:3]) or '-'} | "
            f"hints={','.join(json_list(row['diff_hints'])) or row['audit_signal'] or '-'}"
        )


def cmd_route(args):
    workspace = workspace_path(args.workspace)
    conn = connect(resolve_db(workspace, args.db))
    row = route_for(conn, normalize_path(workspace, args.path))
    print(f"{row['skill']} | pattern={row['pattern']} | {row['reason']}")


def cmd_context(args):
    workspace = workspace_path(args.workspace)
    conn = connect(resolve_db(workspace, args.db))
    term_path, start, end = split_location(workspace, args.term)
    symbol_rows = []
    path = term_path
    if not (workspace / path).exists():
        symbol_rows = fetch_symbols(conn, args.term, args.limit)
        if symbol_rows:
            path = symbol_rows[0]["path"]
            start = max(1, int(symbol_rows[0]["line"]) - args.window)
            end = int(symbol_rows[0]["line"]) + args.window
    elif start is None:
        start, end = 1, min(args.window * 2 + 1, 80)
    if args.pattern and (workspace / path).exists():
        line = find_pattern_line(workspace, path, args.pattern)
        if line:
            start = max(1, line - args.window)
            end = line + args.window
    print("== 路由 ==")
    row = route_for(conn, path)
    print(f"{row['skill']} | pattern={row['pattern']} | {row['reason']}")
    print("== 符号 ==")
    if symbol_rows:
        print_rows(symbol_rows, ["name", "kind", "path", "line", "container", "signature"])
    else:
        rows = conn.execute(
            "SELECT name, kind, path, line, COALESCE(container, '') AS container, COALESCE(signature, '') AS signature FROM symbols WHERE path = ? ORDER BY line LIMIT ?",
            (path, args.limit),
        ).fetchall()
        print_rows(rows, ["name", "kind", "path", "line", "container", "signature"])
    print("== 源码 ==")
    if start and end:
        source_window(workspace, path, start, end)
    print("== 引用 ==")
    if symbol_rows:
        first = symbol_rows[0]["name"]
        for ref in live_rg_refs(conn, workspace, first, min(args.limit, 10)):
            print(f"{ref['path']}:{ref['line']}: {ref['context']}")
    print("== 测试 ==")
    cmd_tests(argparse.Namespace(workspace=args.workspace, db=args.db, term=Path(path).stem, limit=5))
    print("== commits ==")
    cmd_commits(argparse.Namespace(workspace=args.workspace, db=args.db, term=path, limit=5))


def build_parser():
    parser = argparse.ArgumentParser(description="查询目标无关的源码索引，供二进制软件漏洞审计使用。")
    parser.add_argument("--workspace", default=".", help="目标工作区根目录。")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 索引路径。")
    sub = parser.add_subparsers(required=True)

    p = sub.add_parser("meta")
    p.set_defaults(func=cmd_meta)

    p = sub.add_parser("file")
    p.add_argument("term", nargs="?", default="")
    p.add_argument("--pattern")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_file)

    p = sub.add_parser("symbol")
    p.add_argument("name")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_symbol)

    p = sub.add_parser("refs")
    p.add_argument("name")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--no-rg", action="store_true")
    p.set_defaults(func=cmd_refs)

    p = sub.add_parser("tests")
    p.add_argument("term", nargs="?", default="")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_tests)

    p = sub.add_parser("commits")
    p.add_argument("term", nargs="?", default="")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_commits)

    p = sub.add_parser("route")
    p.add_argument("path")
    p.set_defaults(func=cmd_route)

    p = sub.add_parser("context")
    p.add_argument("term")
    p.add_argument("--pattern")
    p.add_argument("--window", type=int, default=8)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_context)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
