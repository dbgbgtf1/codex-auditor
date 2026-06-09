#!/usr/bin/env python3
import argparse
import json
import subprocess
import os
import signal
import shutil
import shlex
import sqlite3
import re
import sys
import time
import datetime as _dt
from pwn import log
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "manager" / "config.json"
SEVERITIES = ("low", "medium", "high")
MINING_TARGET_POLL_SECONDS = 60
LIMIT = 1

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


def resolve_rooted_path(value):
    path = Path(str(value)).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def resolve_relative_path(path):
    path = Path(path)
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def state_dir(cfg):
    return resolve_rooted_path(cfg["state_dir"])


def db_path(cfg):
    return state_dir(cfg) / "queue.sqlite"


def workspace_root(cfg):
    return resolve_rooted_path(
        cfg.get("workspace_root", DEFAULT_CONFIG["workspace_root"])
    )


def audit_root(cfg):
    return resolve_rooted_path(cfg.get("audit_root", DEFAULT_CONFIG["audit_root"]))


def write_default_config(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with path.open("w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
            f.write("\n")


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        cfg.update(user_cfg)
    return cfg


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


def require_codex_command(cfg):
    command = cfg.get("codex_command")
    if not command:
        raise SystemExit("missing required config: codex_command")
    return command


def open_state(args):
    write_default_config(args.config)
    cfg = load_config(args.config)
    conn = connect_db(cfg)
    init_db(conn)
    require_codex_command(cfg)
    return cfg, conn


def clear_directory_contents(path):
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()

    match = re.match(r"^(?P<target>.*?)\s*[\(（](?P<note>.*)[\)）]\s*$", item)
    if not match:
        return {"target": item, "note": None}

def read_target_file(path):
    return [entry["target"] for entry in read_target_entries(path)]


def parse_target_line(line):
    item = re.sub(r"^\s*(?:\d+[\.)]|[-*])\s*", "", line).strip()
    if not item or item.startswith("#"):
        return None

    match = re.match(r"^(?P<target>.*?)\s*[\(（](?P<note>.*)[\)）]\s*$", item)
    if not match:
        return {"target": item, "note": None}

    target = match.group("target").strip()
    note = match.group("note").strip()
    if not target:
        return None
    return {"target": target, "note": note or None}


def read_target_entries(path):
    targets = []
    seen = set()
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        entry = parse_target_line(line)
        if not entry:
            continue
        target = entry["target"]
        if target in seen:
            continue
        seen.add(target)
        targets.append(entry)
    return targets


def zero_counts():
    counts = {}
    for severity in SEVERITIES:
        counts[severity] = 0
    return counts


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
                "security relevance",
                "安全相关",
            )
        ):
            continue
        if re.search(r"\bhigh\b", line):
            return "high"
        if re.search(r"\bmedium\b", line):
            return "medium"
        if re.search(r"\blow\b", line):
            return "low"
        else:
            log.warn(f"{report_path} doesn't have security relevance")
    return None


def scan_archives(cfg, targets):
    audit = audit_root(cfg)
    audit.mkdir(parents=True, exist_ok=True)
    totals = {target: zero_counts() for target in targets}
    new_totals = {target: zero_counts() for target in targets}

    for target, archive_name, report_path in iter_archived_reports(cfg, targets):
        severity = report_severity(report_path)
        if severity in SEVERITIES:
            totals[target][severity] += 1
        dest = audit / f"{target}-{archive_name}.md"
        if dest.exists():
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(report_path, dest)
        if severity in SEVERITIES:
            new_totals[target][severity] += 1
        else:
            log.warn(f"{report_path} doesn't have security relevance")

    return totals, new_totals


def finish_task(conn, task, exit_code):
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
    task_status = "done" if exit_code == 0 else "failed"
    log.info(f"agent finish {task['scope']}, status={task_status}, exit_code={exit_code}")


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
            finish_task(conn, task, exit_code)
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
                finish_task(conn, task, 124)
                reaped += 1
                continue

        if task["pid"] and not process_alive(task["pid"]):
            finish_task(conn, task, 255)
            reaped += 1
    return reaped


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
            json.dumps(
                {"scope": target, "target": target}, ensure_ascii=False, sort_keys=True
            ),
            now(),
        ),
    )
    conn.commit()
    return cursor.lastrowid


def count_running(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE type = 'mining' AND status = 'running'"
    ).fetchone()[0]


def build_mining_prompt(task):
    target = task["scope"]
    return f"""按照 init.md 完成开源二进制漏洞挖掘任务。目标为 {target}"""


def prepare_target_workspace(cfg, target):
    root = workspace_root(cfg)
    target_workspace = root / target
    if target_workspace.exists():
        return target_workspace
    subprocess.run(
        ["cp", f"{root / 'templates'}/", "-r", str(target_workspace)],
        check=True,
    )
    return target_workspace


def append_target_note(target_workspace, note):
    if not note:
        return

    init_path = target_workspace / "init.md"
    try:
        text = init_path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if note in text:
        return

    with init_path.open("a", encoding="utf-8") as f:
        if text and not text.endswith("\n"):
            f.write("\n")
        f.write(f"\n## 补充说明\n\n{note}\n")


def render_agent_command(cfg, workspace):
    command = cfg["codex_command"]
    values = {
        "workspace": shlex.quote(str(workspace)),
        "model": shlex.quote(str(cfg.get("codex_model", "gpt-5.5"))),
    }
    return command.format(**values)


def start_task(cfg, conn, task, note=None):
    run_dir = state_dir(cfg) / "runs" / f"task-{task['id']:06d}"
    run_dir.mkdir(parents=True, exist_ok=True)
    prompt = build_mining_prompt(task)
    prompt_path = run_dir / "prompt.md"
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    exit_path = run_dir / "exit_code"
    prompt_path.write_text(prompt, encoding="utf-8")

    target_workspace = prepare_target_workspace(cfg, task["scope"])
    append_target_note(target_workspace, note)
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
        (started_at, resolve_relative_path(run_dir), proc.pid, task["id"]),
    )
    conn.commit()
    log.info(f"start agent mining {task['scope']}, id = {task['id']}")


def start_targets(cfg, conn, targets, running, notes=None):
    notes = notes or {}
    running_set = set(running)

    for target in targets:
        if len(running_set) >= LIMIT:
            break
        if target in running_set:
            continue
        task_id = enqueue_target_task(conn, target)
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task:
            start_task(cfg, conn, task, notes.get(target))
            running_set.add(target)


def pipeline_tick(cfg, conn, targets, notes=None):
    reap_tasks(cfg, conn)
    running = running_targets(conn)
    start_targets(cfg, conn, targets, running, notes)
    running = running_targets(conn)
    log.info(f"running {json.dumps(running, ensure_ascii=False)}")

    totals, new_totals = scan_archives(cfg, targets)
    for target in targets:
        counts = totals.get(target, zero_counts())
        new_counts = new_totals.get(target, zero_counts())
        log.info(f"{target}: {counts}, {target}_new: {new_counts}")

    totals, new_totals = scan_archives(cfg, targets)
    for target in targets:
        counts = totals.get(target, zero_counts())
        new_counts = new_totals.get(target, zero_counts())
        log.info(f"{target}: {counts}, {target}_new: {new_counts}")

def start_target_file_loop(cfg, conn, target_file):
    target_file = resolve_rooted_path(target_file)
    log.info(f"start mining, target_file = {target_file}")

    while True:
        try:
            entries = read_target_entries(target_file)
        except OSError as exc:
            log.error(f"read target file failed: {exc}")
            entries = []

        targets = [entry["target"] for entry in entries]
        notes = {
            entry["target"]: entry["note"]
            for entry in entries
            if entry.get("note")
        }
        pipeline_tick(cfg, conn, targets, notes)
        time.sleep(MINING_TARGET_POLL_SECONDS)


def cmd_mining(args):
    cfg, conn = open_state(args)
    start_target_file_loop(
        cfg,
        conn,
        target_file=args.target,
    )


def cmd_collect(args):
    cfg, conn = open_state(args)
    try:
        targets = read_target_file(resolve_rooted_path(args.target))
    except OSError as exc:
        log.error(f"read target file failed: {exc}")
        targets = []

    totals, new_totals = scan_archives(cfg, targets)
    for target in targets:
        counts = totals.get(target, zero_counts())
        new_counts = new_totals.get(target, zero_counts())
        log.info(f"{target}: {counts}, {target}_new: {new_counts}")


def cmd_reset(args):
    write_default_config(args.config)
    cfg = load_config(args.config)

    for path in (audit_root(cfg), state_dir(cfg)):
        clear_directory_contents(path)
        log.info(f"reset {path}")


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
        metavar="{mining, collect, reset}",
    )

    p = sub.add_parser(
        "mining", help="start continus mining agents, targeting at the specific file"
    )
    p.add_argument("target", type=Path, help="target file; one target per line")
    p.set_defaults(func=cmd_mining)

    p = sub.add_parser("collect", help="collect confirmed bugs")
    p.add_argument("target", type=Path, help="target file; one target per line")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("reset", help="clear audit output and manager state")
    p.set_defaults(func=cmd_reset)

    return parser


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
