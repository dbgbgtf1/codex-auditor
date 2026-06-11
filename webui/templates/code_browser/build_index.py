#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
SCHEMA = SCRIPT_DIR / "schema.sql"
DEFAULT_DB = Path(".audit") / "code_browser.sqlite"

SOURCE_EXTS = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".h": "c-header",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".inc": "text",
    ".rs": "rust",
    ".go": "go",
    ".py": "python",
    ".js": "js",
    ".mjs": "js",
    ".ts": "ts",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".rb": "ruby",
    ".php": "php",
    ".sh": "shell",
    ".cmake": "cmake",
    ".gn": "gn",
    ".gni": "gn",
    ".md": "markdown",
    ".proto": "proto",
    ".thrift": "thrift",
    ".idl": "idl",
}

DEFAULT_EXCLUDE_PARTS = {
    ".git",
    ".hg",
    ".svn",
    ".cache",
    "__pycache__",
    "node_modules",
    "target",
    "build",
    "out",
    "dist",
    "vendor",
    "third_party",
}

CPP_CLASS_RE = re.compile(r"^\s*(?:template\s*<[^>]+>\s*)?(class|struct|enum)\s+([A-Za-z_][\w:]*)\b")
CPP_FUNC_RE = re.compile(
    r"^\s*(?:(?:template\s*<[^>]+>\s*)?(?:[A-Za-z_][\w:<>,~*&\s]+\s+)+)?"
    r"([A-Za-z_~][\w:~]*::[A-Za-z_~][\w~]*|[A-Za-z_][\w]*)\s*"
    r"\(([^;{}]*)\)\s*(?:const\s*)?(?:noexcept\s*)?(?:override\s*)?(?:final\s*)?(?:\{|$)"
)
RUST_RE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(fn|struct|enum|trait|impl)\s+([A-Za-z_][\w]*)\b")
GO_FUNC_RE = re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_][\w]*)\s*\(")
GO_TYPE_RE = re.compile(r"^\s*type\s+([A-Za-z_][\w]*)\s+(struct|interface|[A-Za-z_][\w]*)\b")
PY_RE = re.compile(r"^\s*(?:async\s+)?(def|class)\s+([A-Za-z_][\w]*)\s*[\(:]")
JS_RE = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_$][\w$]*)\b")
JAVA_RE = re.compile(r"^\s*(?:public|private|protected|static|final|abstract|\s)+\s*(?:class|interface|enum|void|[A-Za-z_][\w<>\[\], ?]+)\s+([A-Za-z_][\w]*)\s*[\({]")
IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
FLAG_RE = re.compile(r"--[A-Za-z0-9][A-Za-z0-9_-]*(?:=[A-Za-z0-9_./:@+-]+)?")
TEST_PATH_RE = re.compile(r"(^|/)(test|tests|spec|fuzz|fuzzer|unittest|integration)(/|$)", re.I)
SECURITY_TERMS = ("security", "cve", "overflow", "oob", "uaf", "race", "crash", "fuzz", "sanitize", "bounds", "fix")


def resolve_workspace(value: str) -> Path:
    return Path(value).expanduser().resolve()


def rel(workspace: Path, path: Path) -> str:
    return path.resolve().relative_to(workspace).as_posix()


def sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


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


def git_metadata(workspace: Path) -> dict[str, str]:
    head = git_output(workspace, ["rev-parse", "HEAD"]) or ""
    status = git_output(workspace, ["status", "--porcelain"])
    return {
        "workspace": str(workspace),
        "git_head": head,
        "git_dirty": "unknown" if status is None else ("yes" if status else "no"),
        "git_status_count": "unknown" if status is None else str(len(status.splitlines())),
    }


def ensure_schema(conn: sqlite3.Connection):
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))


def reset_db(conn: sqlite3.Connection):
    ensure_schema(conn)
    for table in ("files", "symbols", "refs", "tests", "commits", "routes", "meta"):
        conn.execute(f"DELETE FROM {table}")


def should_skip(path: Path, exclude_parts: set[str]) -> bool:
    return any(part in exclude_parts for part in path.parts)


def iter_files(workspace: Path, paths: list[Path], exclude_parts: set[str], limit: int | None):
    count = 0
    for base in paths:
        if not base.exists():
            continue
        candidates = [base] if base.is_file() else (p for p in base.rglob("*") if p.is_file())
        for path in candidates:
            if path.suffix not in SOURCE_EXTS:
                continue
            try:
                path.relative_to(workspace)
            except ValueError:
                continue
            if should_skip(path, exclude_parts):
                continue
            yield path
            count += 1
            if limit and count >= limit:
                return


def parse_symbols(path: Path, text: str):
    lang = SOURCE_EXTS.get(path.suffix, "text")
    symbols = []
    container = None
    for line_no, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "#", "*")):
            continue
        if lang in {"c", "cpp", "c-header"}:
            m = CPP_CLASS_RE.match(line)
            if m:
                container = m.group(2)
                symbols.append((container, m.group(1), line_no, None, stripped[:300]))
                continue
            m = CPP_FUNC_RE.match(line)
            if m and m.group(1) not in {"if", "for", "while", "switch", "return", "sizeof"}:
                symbols.append((m.group(1), "function", line_no, container, stripped[:300]))
        elif lang == "rust":
            m = RUST_RE.match(line)
            if m:
                symbols.append((m.group(2), m.group(1), line_no, container, stripped[:300]))
        elif lang == "go":
            m = GO_FUNC_RE.match(line)
            if m:
                symbols.append((m.group(1), "function", line_no, container, stripped[:300]))
                continue
            m = GO_TYPE_RE.match(line)
            if m:
                symbols.append((m.group(1), m.group(2), line_no, container, stripped[:300]))
        elif lang == "python":
            m = PY_RE.match(line)
            if m:
                symbols.append((m.group(2), m.group(1), line_no, container, stripped[:300]))
        elif lang in {"js", "ts"}:
            m = JS_RE.match(line)
            if m:
                symbols.append((m.group(1), "symbol", line_no, container, stripped[:300]))
        elif lang == "java":
            m = JAVA_RE.match(line)
            if m:
                symbols.append((m.group(1), "java-symbol", line_no, container, stripped[:300]))
    return symbols


def parse_refs(text: str, max_per_file: int):
    refs = []
    seen = set()
    skip = {"const", "return", "static", "inline", "class", "struct", "public", "private", "protected", "function", "import"}
    for line_no, line in enumerate(text.splitlines(), 1):
        if len(refs) >= max_per_file:
            break
        if len(line) > 800:
            continue
        for name in IDENT_RE.findall(line):
            if name in skip:
                continue
            key = (name, line_no)
            if key in seen:
                continue
            seen.add(key)
            refs.append((name, line_no, line.strip()[:300]))
    return refs


def parse_test(workspace: Path, path: Path, text: str):
    rpath = rel(workspace, path)
    name = path.name.lower()
    is_test = bool(TEST_PATH_RE.search(rpath)) or name.endswith(("_test.go", "_test.rs", "_test.py", "test.js", "spec.js"))
    if not is_test:
        return None
    tags = []
    for token in ("assert", "expect", "panic", "crash", "fuzz", "sanitize", "asan", "ubsan", "regress", "CVE", "timeout"):
        if token.lower() in text.lower():
            tags.append(token)
    flags = sorted(set(FLAG_RE.findall(text)))
    source_hint = None
    match = re.search(r"(src|lib|include)/[A-Za-z0-9_./+-]+", text)
    if match:
        source_hint = match.group(0)
    return rpath, json.dumps(sorted(set(tags))), json.dumps(flags), source_hint


def load_routes(route_file: Path | None):
    if not route_file:
        return []
    data = json.loads(route_file.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit("route file 必须是 JSON 数组")
    routes = []
    for item in data:
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("pattern") or "").strip()
        skill = str(item.get("skill") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if pattern and skill:
            routes.append((pattern, skill, reason))
    return routes


def classify_file(path: str, test_roots: set[str]) -> tuple[bool, bool]:
    lower = path.lower()
    is_test = bool(TEST_PATH_RE.search(lower)) or any(lower.startswith(root.rstrip("/") + "/") for root in test_roots)
    is_source = not is_test
    return is_source, is_test


def commit_hints(subject: str, files: list[str]) -> tuple[list[str], str]:
    haystack = (subject + " " + " ".join(files)).lower()
    hints = [term for term in SECURITY_TERMS if term in haystack]
    signal = ",".join(hints[:5])
    return hints, signal


def index_commits(conn: sqlite3.Connection, workspace: Path, max_commits: int, test_roots: set[str]):
    if max_commits <= 0 or not (workspace / ".git").exists():
        return
    raw = git_output(
        workspace,
        ["log", f"-n{max_commits}", "--date=iso-strict", "--name-only", "--format=%H%x09%ad%x09%s"],
    )
    if not raw:
        return
    current = None
    commits = []
    for line in raw.splitlines():
        if "\t" in line:
            if current:
                commits.append(current)
            parts = line.split("\t", 2)
            current = {"hash": parts[0], "date": parts[1] if len(parts) > 1 else "", "subject": parts[2] if len(parts) > 2 else "", "files": []}
        elif current is not None and line.strip():
            current["files"].append(line.strip())
    if current:
        commits.append(current)

    for item in commits:
        source_files = []
        test_files = []
        for file_name in item["files"]:
            is_source, is_test = classify_file(file_name, test_roots)
            if is_source:
                source_files.append(file_name)
            if is_test:
                test_files.append(file_name)
        hints, signal = commit_hints(item["subject"], item["files"])
        conn.execute(
            """
            INSERT OR REPLACE INTO commits(hash, subject, date, files, source_files, test_files, diff_hints, audit_signal)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["hash"],
                item["subject"],
                item["date"],
                json.dumps(item["files"]),
                json.dumps(source_files),
                json.dumps(test_files),
                json.dumps(hints),
                signal,
            ),
        )


def main():
    parser = argparse.ArgumentParser(description="构建目标无关的源码索引，供二进制软件漏洞审计使用。")
    parser.add_argument("--workspace", default=".", help="目标工作区根目录。")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 索引路径。")
    parser.add_argument("--path", action="append", default=[], help="要索引的路径，可为相对工作区路径或绝对路径，可重复。")
    parser.add_argument("--test-root", action="append", default=["test", "tests", "spec", "fuzz"], help="相对测试根目录提示，可重复。")
    parser.add_argument("--exclude-part", action="append", default=[], help="要跳过的路径组件，可重复。")
    parser.add_argument("--route-file", type=Path, help="包含 pattern/skill/reason 条目的 JSON 路由文件。")
    parser.add_argument("--refs-per-file", type=int, default=0, help="每个文件最多预缓存 N 条文本引用。")
    parser.add_argument("--max-commits", type=int, default=500)
    parser.add_argument("--limit", type=int, help="Limit indexed files for quick validation.")
    args = parser.parse_args()

    workspace = resolve_workspace(args.workspace)
    db = args.db if args.db.is_absolute() else workspace / args.db
    db.parent.mkdir(parents=True, exist_ok=True)

    paths = [workspace / p for p in args.path] if args.path else [workspace]
    paths = [p if p.is_absolute() else workspace / p for p in paths]
    exclude_parts = set(DEFAULT_EXCLUDE_PARTS) | set(args.exclude_part)
    test_roots = {root.strip("/") for root in args.test_root}

    conn = sqlite3.connect(db)
    reset_db(conn)
    for pattern, skill, reason in load_routes(args.route_file):
        conn.execute("INSERT OR REPLACE INTO routes(pattern, skill, reason) VALUES (?, ?, ?)", (pattern, skill, reason))

    file_count = symbol_count = ref_count = test_count = 0
    for path in iter_files(workspace, paths, exclude_parts, args.limit):
        data = path.read_bytes()
        text = data.decode("utf-8", "replace")
        rpath = rel(workspace, path)
        lang = SOURCE_EXTS.get(path.suffix, "text")
        stat = path.stat()
        conn.execute(
            "INSERT OR REPLACE INTO files(path, lang, sha1, size, mtime) VALUES (?, ?, ?, ?, ?)",
            (rpath, lang, sha1_bytes(data), stat.st_size, stat.st_mtime),
        )
        file_count += 1
        for name, kind, line, container, signature in parse_symbols(path, text):
            conn.execute(
                "INSERT INTO symbols(name, kind, path, line, container, signature) VALUES (?, ?, ?, ?, ?, ?)",
                (name, kind, rpath, line, container, signature),
            )
            symbol_count += 1
        for name, line, context in parse_refs(text, args.refs_per_file):
            conn.execute(
                "INSERT OR IGNORE INTO refs(name, path, line, context) VALUES (?, ?, ?, ?)",
                (name, rpath, line, context),
            )
            ref_count += 1
        parsed_test = parse_test(workspace, path, text)
        if parsed_test:
            conn.execute("INSERT OR REPLACE INTO tests(path, tags, flags, source_hint) VALUES (?, ?, ?, ?)", parsed_test)
            test_count += 1

    index_commits(conn, workspace, args.max_commits, test_roots)
    meta = {
        **git_metadata(workspace),
        "indexed_paths": json.dumps([rel(workspace, p) if p.is_relative_to(workspace) else str(p) for p in paths]),
        "test_roots": json.dumps(sorted(test_roots)),
        "route_file": str(args.route_file or ""),
        "file_count": str(file_count),
        "symbol_count": str(symbol_count),
        "ref_count": str(ref_count),
        "test_count": str(test_count),
    }
    for key, value in meta.items():
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    print(f"已索引 files={file_count} symbols={symbol_count} refs={ref_count} tests={test_count} db={db}")


if __name__ == "__main__":
    main()
