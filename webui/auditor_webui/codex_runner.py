"""Codex process orchestration for WebUI sessions."""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import cast

from .config import CONFIG, UUID_RE
from .database import (
    add_message,
    create_run,
    get_session,
    increment_auto_continue,
    latest_user_input,
    now_iso,
    reset_auto_continue,
    update_run_result,
    update_session_status,
)
from .prompts import auto_continue_prompt, base_prompt
from .schema import (
    BUSY_STATUSES,
    JUDGE_CONTINUE_MESSAGE,
    JUDGE_STOP_MESSAGE,
    JsonObject,
    JsonValue,
    JudgeResult,
    RowDict,
    row_int,
    row_optional_str,
    row_str,
)

ACTIVE_RUNS: dict[int, subprocess.Popen[str]] = {}
STOP_REQUESTS: set[int] = set()
ACTIVE_LOCK = threading.Lock()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def load_env_file(path: Path = Path("/etc/audit-env")) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line.startswith("export ") or "=" not in line:
            continue
        try:
            token = shlex.split(line[len("export ") :], posix=True)[0]
        except (IndexError, ValueError):
            continue
        key, value = token.split("=", 1)
        if key:
            env[key] = value
    return env


def codex_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update({key: value for key, value in load_env_file().items() if value})
    env["CODEX_HOME"] = str(CONFIG.codex_home)
    return env


def build_codex_command(
    *,
    model: str,
    output_file: Path,
    resume_session_id: str | None,
    json_events: bool = True,
    ephemeral: bool = False,
) -> list[str]:
    cmd = ["codex", "exec", "resume"] if resume_session_id else ["codex", "exec"]
    if json_events:
        cmd.append("--json")
    if ephemeral:
        cmd.append("--ephemeral")
    cmd.append("--dangerously-bypass-approvals-and-sandbox")
    cmd.append("--skip-git-repo-check")
    if not resume_session_id:
        cmd.extend(["-C", str(CONFIG.workspace)])
    cmd.extend(["-m", model, "-o", str(output_file)])
    if resume_session_id:
        cmd.extend([resume_session_id, "-"])
    else:
        cmd.append("-")
    return cmd


def recursive_find_uuid(value: JsonValue) -> str | None:
    if isinstance(value, str):
        match = UUID_RE.search(value)
        return match.group(0) if match else None
    if isinstance(value, dict):
        for key in ("session_id", "conversation_id", "thread_id", "id"):
            if key in value:
                found = recursive_find_uuid(value[key])
                if found:
                    return found
        for item in value.values():
            found = recursive_find_uuid(item)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = recursive_find_uuid(item)
            if found:
                return found
    return None


def extract_event_note(event: JsonObject) -> str | None:
    event_type = str(event.get("type", ""))
    if event_type in {"agent_message", "assistant_message"}:
        for key in ("message", "text", "content"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if event_type in {"error", "turn.failed", "failure"}:
        for key in ("message", "error", "reason"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return f"Codex 事件错误: {value.strip()}"
    return None


def write_json_event_log(log_file: Path, line: str) -> None:
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(line)
        if not line.endswith("\n"):
            handle.write("\n")


def discover_latest_session_id(start_time: float) -> str | None:
    sessions_dir = CONFIG.codex_home / "sessions"
    if not sessions_dir.exists():
        return None
    candidates: list[tuple[float, Path]] = []
    for path in sessions_dir.rglob("*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= start_time - 5:
            candidates.append((mtime, path))
    candidates.sort(reverse=True)
    for _, path in candidates[:10]:
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                for _ in range(80):
                    line = handle.readline()
                    if not line:
                        break
                    match = UUID_RE.search(line)
                    if match:
                        return match.group(0)
        except OSError:
            continue
    return None


def run_subprocess(cmd: list[str], prompt: str, *, env: dict[str, str]) -> tuple[int, str]:
    proc = subprocess.run(
        cmd,
        input=prompt,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(CONFIG.workspace),
        env=env,
        timeout=120,
        check=False,
    )
    return proc.returncode, proc.stdout


def strip_json_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    return value


def parse_judge_text(text: str) -> JudgeResult:
    cleaned = strip_json_fence(text)
    parsed: JsonValue = {}
    try:
        parsed = cast(JsonValue, json.loads(cleaned))
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                parsed = cast(JsonValue, json.loads(match.group(0)))
            except json.JSONDecodeError:
                parsed = {}
    if isinstance(parsed, dict) and "continue" in parsed:
        return {
            "continue_": bool(parsed.get("continue")),
            "reason": str(parsed.get("reason", "")).strip() or "停顿判断器未给出原因。",
            "report": str(parsed.get("report", "")).strip(),
            "source": "model",
        }
    return heuristic_judge(text)


def heuristic_judge(text: str) -> JudgeResult:
    lowered = text.lower()
    stop_markers = [
        "需要用户",
        "请提供",
        "无法继续",
        "需要你",
        "等待",
        "api key",
        "authentication",
        "permission denied",
        "任务已完成",
        "已经完成",
    ]
    should_stop = any(marker in lowered or marker in text for marker in stop_markers)
    return {
        "continue_": not should_stop,
        "reason": "模型判断不可用，使用本地关键词启发式判断。",
        "report": truncate(text.strip(), 800),
        "source": "heuristic",
    }


def judge_should_continue(session: RowDict, last_message: str, user_input: str) -> JudgeResult:
    if not last_message.strip():
        return {"continue_": False, "reason": "主 agent 没有返回可判断的最终消息。", "report": "", "source": "local"}

    schema_path = CONFIG.temp_dir / "judge-schema.json"
    schema_path.write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["continue", "reason", "report"],
                "properties": {
                    "continue": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "report": {"type": "string"},
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    output_file = CONFIG.temp_dir / f"judge-{uuid.uuid4()}.txt"
    prompt = f"""你是 codex-auditor WebUI 的停顿判断器。不要运行工具，只输出 JSON。

判断主 agent 停下来后是否应该自动继续:
- 如果主 agent 只是阶段性总结、还有明确未完成的审计路径、还未维护标识符-000 报告，continue=true。
- 如果主 agent 已完成用户目标、明确要求用户提供必要信息、缺少认证或外部输入导致无法继续、
  需要人工确认高风险操作，continue=false。

目标标识符: {row_str(session, "identifier")}
用户输入:
{user_input or "未记录"}

主 agent 最终消息:
{last_message}

输出 JSON 字段:
{{"continue": true/false, "reason": "一句话原因", "report": "需要显示到 WebUI 的用户可读信息"}}"""
    cmd = build_codex_command(
        model=CONFIG.judge_model,
        output_file=output_file,
        resume_session_id=None,
        json_events=True,
        ephemeral=True,
    )
    cmd.insert(-1, "--output-schema")
    cmd.insert(-1, str(schema_path))
    try:
        returncode, stdout = run_subprocess(cmd, prompt, env=codex_env())
    except (OSError, subprocess.SubprocessError) as exc:
        result = heuristic_judge(last_message)
        result["reason"] = f"停顿判断器启动失败，使用启发式判断: {exc}"
        return result

    judge_text = ""
    if output_file.exists():
        judge_text = output_file.read_text(encoding="utf-8", errors="ignore").strip()
    if not judge_text:
        judge_text = stdout.strip()
    result = parse_judge_text(judge_text)
    if returncode != 0:
        result["reason"] = f"停顿判断器返回非零状态 {returncode}，{result['reason']}"
    return result


def is_session_active(session_id: int) -> bool:
    with ACTIVE_LOCK:
        proc = ACTIVE_RUNS.get(session_id)
    return bool(proc and proc.poll() is None)


def start_agent_run(session_id: int, prompt: str, *, source: str) -> bool:
    with ACTIVE_LOCK:
        if session_id in ACTIVE_RUNS:
            return False
        STOP_REQUESTS.discard(session_id)
    session = get_session(session_id)
    if not session:
        raise KeyError("会话不存在")
    if row_str(session, "status") in BUSY_STATUSES:
        return False

    started_at = now_iso()
    run_id = create_run(
        session_id=session_id,
        source=source,
        prompt=prompt,
        model=CONFIG.main_model,
        started_at=started_at,
        codex_session_id_before=row_optional_str(session, "codex_session_id"),
    )

    thread = threading.Thread(target=agent_worker, args=(session_id, run_id, prompt, source), daemon=True)
    thread.start()
    return True


def agent_worker(session_id: int, run_id: int, prompt: str, source: str) -> None:
    session = get_session(session_id)
    if not session:
        return
    start_time = time.time()
    first_turn = not bool(row_optional_str(session, "codex_session_id"))
    rendered_prompt = base_prompt(session, prompt, first_turn=first_turn)
    output_file = CONFIG.temp_dir / f"last-{run_id}.txt"
    log_file = CONFIG.temp_dir / f"events-{run_id}.jsonl"
    cmd = build_codex_command(
        model=CONFIG.main_model,
        output_file=output_file,
        resume_session_id=row_optional_str(session, "codex_session_id"),
    )
    returncode = 1
    error: str | None = None
    discovered_session_id: str | None = None
    event_notes: list[str] = []
    stop_requested = False
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(CONFIG.workspace),
            env=codex_env(),
            bufsize=1,
        )
        with ACTIVE_LOCK:
            ACTIVE_RUNS[session_id] = proc
        if proc.stdin is None or proc.stdout is None:
            raise RuntimeError("Codex 子进程管道未创建")
        proc.stdin.write(rendered_prompt)
        proc.stdin.close()

        for line in proc.stdout:
            write_json_event_log(log_file, line)
            try:
                event = cast(JsonValue, json.loads(line))
            except json.JSONDecodeError:
                continue
            found_uuid = recursive_find_uuid(event)
            if found_uuid:
                discovered_session_id = found_uuid
            if isinstance(event, dict):
                note = extract_event_note(event)
                if note:
                    event_notes.append(note)
        returncode = proc.wait()
    except OSError as exc:
        error = f"无法启动 codex: {exc}"
        returncode = 127
    except Exception as exc:
        error = f"Codex 执行异常: {exc}"
        returncode = 1
    finally:
        with ACTIVE_LOCK:
            ACTIVE_RUNS.pop(session_id, None)
            stop_requested = session_id in STOP_REQUESTS
            STOP_REQUESTS.discard(session_id)

    finalize_agent_run(
        session=session,
        session_id=session_id,
        run_id=run_id,
        source=source,
        output_file=output_file,
        event_notes=event_notes,
        discovered_session_id=discovered_session_id,
        start_time=start_time,
        returncode=returncode,
        error=error,
        stop_requested=stop_requested,
    )


def finalize_agent_run(
    *,
    session: RowDict,
    session_id: int,
    run_id: int,
    source: str,
    output_file: Path,
    event_notes: list[str],
    discovered_session_id: str | None,
    start_time: float,
    returncode: int,
    error: str | None,
    stop_requested: bool,
) -> None:
    last_message = ""
    if output_file.exists():
        last_message = output_file.read_text(encoding="utf-8", errors="ignore").strip()
    if not last_message and event_notes:
        last_message = "\n\n".join(event_notes[-3:]).strip()
    if not discovered_session_id:
        discovered_session_id = discover_latest_session_id(start_time)

    ended_at = now_iso()
    if last_message:
        add_message(session_id, "assistant", last_message, "message")
    interrupted = stop_requested or returncode == -signal.SIGTERM
    if error and not interrupted:
        add_message(session_id, "system", error, "error")
    elif returncode and returncode != 0 and not interrupted:
        add_message(session_id, "system", f"Codex 主 agent 返回非零状态 {returncode}。", "error")

    judge_result: JudgeResult | None = None
    should_auto_continue = False
    latest_session = get_session(session_id) or session
    if returncode == 0 and row_int(latest_session, "auto_continue") and last_message:
        update_session_status(session_id, "judging")
        judge_result = judge_should_continue(latest_session, last_message, latest_user_input(session_id))
        should_auto_continue = judge_result["continue_"]

    last_error = error or (None if interrupted else (f"Codex 返回状态 {returncode}" if returncode else None))
    update_run_result(
        session_id=session_id,
        run_id=run_id,
        status="completed" if returncode == 0 else ("interrupted" if interrupted else "failed"),
        returncode=returncode,
        ended_at=ended_at,
        codex_session_id_after=discovered_session_id,
        last_message=last_message,
        judge_result_json=json.dumps(judge_result, ensure_ascii=False) if judge_result else None,
        error=error,
        last_error=last_error,
        last_stop_reason=judge_result["reason"] if judge_result else None,
    )

    if should_auto_continue and judge_result:
        updated = get_session(session_id)
        if updated and row_int(updated, "auto_continue_count") < CONFIG.max_auto_continue:
            increment_auto_continue(session_id)
            add_message(session_id, "judge", JUDGE_CONTINUE_MESSAGE, "judge")
            start_agent_run(session_id, auto_continue_prompt(judge_result["reason"]), source="auto")
        else:
            add_message(session_id, "judge", JUDGE_STOP_MESSAGE, "judge")
    elif judge_result:
        add_message(session_id, "judge", JUDGE_STOP_MESSAGE, "judge")
    elif source == "user":
        reset_auto_continue(session_id)


def stop_agent_run(session_id: int) -> bool:
    with ACTIVE_LOCK:
        proc = ACTIVE_RUNS.get(session_id)
    if not proc or proc.poll() is not None:
        return False
    with ACTIVE_LOCK:
        STOP_REQUESTS.add(session_id)
    proc.send_signal(signal.SIGTERM)
    return True
