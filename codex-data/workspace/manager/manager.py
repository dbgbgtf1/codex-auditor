#!/usr/bin/env python3
import argparse
import datetime as _dt
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "manager" / "config.json"

MINING_TARGET_POLL_SECONDS = 30
DEFAULT_MINING_LIMIT = 3
SEVERITIES = ("low", "medium", "high")

DEFAULT_CONFIG = {
    "workspace_root": "/data/workspace",
    "audit_root": "/data/workspace/audit",
    "state_dir": "manager/state",
    "task_timeout_sec": 14400,
    "codex_model": "gpt-5.5",
    "codex_command": (
        "codex exec --model {model} -C {workspace} "
        "--dangerously-bypass-approvals-and-sandbox -"
    ),
}


def now():
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def rel(path):
    path = Path(path)
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def resolve_rooted_path(value):
    path = Path(str(value)).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        cfg.update(user_cfg)
    return cfg


def write_default_config(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with path.open("w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
            f.write("\n")


def workspace_root(cfg):
    return resolve_rooted_path(cfg.get("workspace_root", DEFAULT_CONFIG["workspace_root"]))


def audit_root(cfg):
    return resolve_rooted_path(cfg.get("audit_root", DEFAULT_CONFIG["audit_root"]))


def state_dir(cfg):
    return resolve_rooted_path(cfg["state_dir"])


def db_path(cfg):
    return state_dir(cfg) / "queue.sqlite"


def connect_db(cfg):
    sd = state_dir(cfg)
    sd.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(cfg))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          type TEXT NOT NULL,
          status TEXT NOT NULL,
          bug_dir TEXT,
          scope TEXT,
          attempt_id TEXT,
          payload TEXT,
          run_dir TEXT,
          pid INTEGER,
          created_at TEXT NOT NULL,
          started_at TEXT,
          finished_at TEXT,
          exit_code INTEGER,
          last_error TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_status_type
          ON tasks(status, type, id);
        """
    )
    conn.commit()


def open_state(args):
    write_default_config(args.config)
    cfg = load_config(args.config)
    conn = connect_db(cfg)
    init_db(conn)
    return cfg, conn


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


def log_line(message):
    print(f"[{now()}] {message}", flush=True)


def zero_counts():
    return {severity: 0 for severity in SEVERITIES}


def report_severity(report_path):
    try:
        text = report_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None

    for raw_line in text.splitlines():
        line = raw_line.casefold()
        if not any(
            marker in line
            for marker in (
                "security_relevance",
                "security relevance",
                "severity",
                "安全相关",
                "严重",
                "高危",
                "中危",
                "低危",
            )
        ):
            continue
        if re.search(r"\bhigh\b", line) or "高危" in raw_line or "高" in raw_line:
            return "high"
        if re.search(r"\bmedium\b", line) or "中危" in raw_line or "中" in raw_line:
            return "medium"
        if re.search(r"\blow\b", line) or "低危" in raw_line or "低" in raw_line:
            return "low"
    return None


def iter_archived_reports(cfg, targets):
    root = workspace_root(cfg)
    for target in targets:
        archives = root / target / "archives"
        if not archives.exists() or not archives.is_dir():
            continue
        for archive_dir in sorted(archives.iterdir(), key=lambda path: path.name):
            if not archive_dir.is_dir() or archive_dir.is_symlink():
                continue
            report_path = archive_dir / "report.md"
            if report_path.is_file():
                yield target, archive_dir.name, report_path


def scan_archives(cfg, targets):
    audit = audit_root(cfg)
    audit.mkdir(parents=True, exist_ok=True)
    totals = {target: zero_counts() for target in targets}
    new_totals = {target: zero_counts() for target in targets}
    copied = []

    for target, archive_name, report_path in iter_archived_reports(cfg, targets):
        severity = report_severity(report_path)
        if severity in SEVERITIES:
            totals[target][severity] += 1

        dest = audit / f"{target}-{archive_name}.md"
        if dest.exists():
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(report_path, dest)
        copied.append(dest)
        if severity in SEVERITIES:
            new_totals[target][severity] += 1

    return totals, new_totals, copied


def read_target_file(path):
    targets = []
    seen = set()
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        item = re.sub(r"^\s*(?:\d+[\.)]|[-*])\s*", "", line).strip()
        if not item or item.startswith("#"):
            continue
        if item in seen:
            continue
        seen.add(item)
        targets.append(item)
    return targets


def discover_targets(cfg):
    root = workspace_root(cfg)
    if not root.exists() or not root.is_dir():
        return []
    targets = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.name == "audit":
            continue
        if path.is_dir() and not path.is_symlink() and (path / "archives").is_dir():
            targets.append(path.name)
    return targets


def render_agent_command(cfg, workspace):
    command = str(cfg.get("codex_command") or DEFAULT_CONFIG["codex_command"])
    values = {
        "workspace": shlex.quote(str(workspace)),
        "model": shlex.quote(str(cfg.get("codex_model", "gpt-5.5"))),
    }
    return command.format(**values)


def prepare_target_workspace(cfg, target):
    root = workspace_root(cfg)
    subprocess.run(
        ["cp", f"{root / 'templates'}/", "-r", str(root / target)],
        check=True,
    )
    return root / target


def build_mining_prompt(task):
    target = task["scope"] or "未指定"
    return f"""按照 init.md 完成开源二进制漏洞挖掘任务。目标为 {target}"""


def task_label(task):
    parts = [f"task #{task['id']}", task["type"]]
    if task["scope"]:
        parts.append(f"scope={task['scope']}")
    return " ".join(parts)


def log_agent_start(task, pid, run_dir, prompt):
    log_line(f"启动 agent {task_label(task)} pid={pid} run={rel(run_dir)}")
    print(f"----- task #{task['id']} prompt begin -----", flush=True)
    print(prompt.rstrip(), flush=True)
    print(f"----- task #{task['id']} prompt end -----", flush=True)


def log_agent_finish(task, exit_code):
    task_status = "done" if exit_code == 0 else "failed"
    log_line(f"agent 结束 {task_label(task)} status={task_status} exit_code={exit_code}")


def count_running(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE type = 'mining' AND status = 'running'"
    ).fetchone()[0]


def running_targets(conn):
    rows = conn.execute(
        """
        SELECT scope FROM tasks
        WHERE type = 'mining' AND status = 'running'
        ORDER BY id ASC
        """
    ).fetchall()
    return [row["scope"] for row in rows if row["scope"]]


def enqueue_target_task(conn, target):
    cursor = conn.execute(
        """
        INSERT INTO tasks (type, status, scope, payload, created_at)
        VALUES ('mining', 'queued', ?, ?, ?)
        """,
        (
            target,
            json.dumps({"scope": target, "target": target}, ensure_ascii=False, sort_keys=True),
            now(),
        ),
    )
    conn.commit()
    return cursor.lastrowid


def start_task(cfg, conn, task, running_limit=None):
    if running_limit is not None and count_running(conn) >= running_limit:
        return False

    run_dir = state_dir(cfg) / "runs" / f"task-{task['id']:06d}"
    run_dir.mkdir(parents=True, exist_ok=True)
    prompt = build_mining_prompt(task)
    prompt_path = run_dir / "prompt.md"
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    exit_path = run_dir / "exit_code"
    prompt_path.write_text(prompt, encoding="utf-8")

    target_workspace = prepare_target_workspace(cfg, task["scope"])
    command = render_agent_command(cfg, target_workspace)
    script = (
        f"{command} < {shlex.quote(str(prompt_path))} "
        f"> {shlex.quote(str(stdout_path))} "
        f"2> {shlex.quote(str(stderr_path))}\n"
        f"printf '%s\\n' \"$?\" > {shlex.quote(str(exit_path))}\n"
    )
    script_path = run_dir / "run.sh"
    script_path.write_text(script, encoding="utf-8")
    script_path.chmod(0o755)

    proc = subprocess.Popen(
        ["/bin/sh", str(script_path)],
        cwd=ROOT,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    started_at = now()
    conn.execute(
        """
        UPDATE tasks
        SET status = 'running', started_at = ?, run_dir = ?, pid = ?
        WHERE id = ?
        """,
        (started_at, rel(run_dir), proc.pid, task["id"]),
    )
    conn.commit()
    metadata = {
        "task_id": task["id"],
        "type": "mining",
        "scope": task["scope"],
        "pid": proc.pid,
        "started_at": started_at,
        "command": command,
    }
    write_json(run_dir / "metadata.json", metadata)
    log_agent_start(task, proc.pid, run_dir, prompt)
    return True


def finish_task(cfg, conn, task, exit_code):
    task_status = "done" if exit_code == 0 else "failed"
    last_error = None if exit_code == 0 else f"runner exit code {exit_code}"
    conn.execute(
        """
        UPDATE tasks
        SET status = ?, finished_at = ?, exit_code = ?, last_error = ?
        WHERE id = ?
        """,
        (task_status, now(), exit_code, last_error, task["id"]),
    )
    conn.commit()
    log_agent_finish(task, exit_code)


def process_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def reap_tasks(cfg, conn):
    rows = conn.execute(
        "SELECT * FROM tasks WHERE status = 'running' AND type = 'mining'"
    ).fetchall()
    reaped = 0
    timeout = int(cfg.get("task_timeout_sec", 14400))
    for task in rows:
        run_dir = ROOT / task["run_dir"] if task["run_dir"] else None
        exit_path = run_dir / "exit_code" if run_dir else None
        if exit_path and exit_path.exists():
            try:
                exit_code = int(exit_path.read_text(encoding="utf-8").strip())
            except Exception:
                exit_code = 255
            finish_task(cfg, conn, task, exit_code)
            reaped += 1
            continue

        if task["started_at"]:
            try:
                started = _dt.datetime.fromisoformat(task["started_at"])
                age = (_dt.datetime.now(_dt.timezone.utc) - started).total_seconds()
            except Exception:
                age = 0
            if age > timeout:
                pid = task["pid"]
                if pid:
                    try:
                        os.killpg(pid, signal.SIGTERM)
                    except Exception:
                        pass
                finish_task(cfg, conn, task, 124)
                reaped += 1
                continue

        if task["pid"] and not process_alive(task["pid"]):
            finish_task(cfg, conn, task, 255)
            reaped += 1
    return reaped


def start_targets(cfg, conn, targets, limit, already_running):
    capacity = max(0, int(limit) - len(already_running))
    running_set = set(already_running)
    started = []

    for target in targets:
        if len(started) >= capacity:
            break
        if target in running_set:
            continue
        task_id = enqueue_target_task(conn, target)
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task and start_task(cfg, conn, task, running_limit=int(limit)):
            started.append(target)
    return started


def format_counts(counts):
    return (
        "{ "
        f"low: {counts.get('low', 0)}, "
        f"medium: {counts.get('medium', 0)}, "
        f"high: {counts.get('high', 0)}"
        " }"
    )


def log_tick_summary(targets, running, starting, totals, new_totals):
    print(
        f"running {json.dumps(running, ensure_ascii=False)}, "
        f"starting {json.dumps(starting, ensure_ascii=False)}",
        flush=True,
    )
    for target in targets:
        print(
            f"{target}: {format_counts(totals.get(target, zero_counts()))}, "
            f"{target}_new: {format_counts(new_totals.get(target, zero_counts()))}",
            flush=True,
        )


def pipeline_tick(cfg, conn, targets, limit=None):
    if limit is None:
        limit = DEFAULT_MINING_LIMIT
    reap_tasks(cfg, conn)
    running = running_targets(conn)
    starting = start_targets(cfg, conn, targets, limit, running)
    totals, new_totals, copied = scan_archives(cfg, targets)
    return {
        "running": running,
        "starting": starting,
        "totals": totals,
        "new_totals": new_totals,
        "copied": [rel(path) for path in copied],
    }


def start_target_file_loop(cfg, conn, target_file, limit):
    target_file = resolve_rooted_path(target_file)
    log_line(
        f"mining loop 启动 target_file={target_file} "
        f"limit={limit} poll={MINING_TARGET_POLL_SECONDS}s"
    )
    while True:
        try:
            targets = read_target_file(target_file)
        except OSError as exc:
            log_line(f"读取 target file 失败: {exc}")
            targets = []

        tick = pipeline_tick(cfg, conn, targets, limit=limit)
        log_tick_summary(
            targets,
            tick["running"],
            tick["starting"],
            tick["totals"],
            tick["new_totals"],
        )
        time.sleep(MINING_TARGET_POLL_SECONDS)


def cmd_mining(args):
    cfg, conn = open_state(args)
    start_target_file_loop(
        cfg,
        conn,
        target_file=args.targets,
        limit=args.limit,
    )


def cmd_tick(args):
    cfg, conn = open_state(args)
    if args.targets:
        targets = read_target_file(resolve_rooted_path(args.targets))
    else:
        targets = discover_targets(cfg)
    tick = pipeline_tick(cfg, conn, targets, limit=args.limit)
    log_tick_summary(
        targets,
        tick["running"],
        tick["starting"],
        tick["totals"],
        tick["new_totals"],
    )


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Open-source binary audit manager")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="config JSON path",
    )
    sub = parser.add_subparsers(
        required=True,
        metavar="{mining,tick}",
    )

    p = sub.add_parser("mining", help="start continuous mining agents from a target file")
    p.add_argument("targets", type=Path, help="target file; one target per line, optionally numbered")
    p.add_argument("--limit", type=int, default=DEFAULT_MINING_LIMIT, help="maximum agents to run")
    p.set_defaults(func=cmd_mining)

    p = sub.add_parser("tick", help="run one scheduled tick")
    p.add_argument("targets", nargs="?", type=Path, help="optional target file")
    p.add_argument("--limit", type=int, default=DEFAULT_MINING_LIMIT)
    p.set_defaults(func=cmd_tick)

    return parser


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
