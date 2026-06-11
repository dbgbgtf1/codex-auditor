"""Prompt templates used by the automation worker."""

from __future__ import annotations

from .config import CONFIG
from .schema import RowDict, row_str


def base_prompt(session: RowDict, user_prompt: str, *, first_turn: bool) -> str:
    target_name = row_str(session, "target_name")
    workspace_path = row_str(session, "target_workspace_path", str(CONFIG.workspace))
    note = row_str(session, "target_note")
    session_type = row_str(session, "session_type", "debug")
    heading = "这是该 WebUI 会话的首次请求。" if first_turn else "这是该 WebUI 会话的后续请求。"
    return f"""你正在执行 codex-auditor 自动化二进制安全审计会话。{heading}

目标: {target_name}
Session 类型: {session_type}
目标工作区: {workspace_path}

必须遵守:
- 阅读并遵守 {CONFIG.agents_path}。
- 当前进程工作目录就是目标工作区，所有产物优先保存在该目录内。
- 阅读并遵守 ./init.md、./bugs.md 和 ./confirm.md。
- 持续维护 ./archives/known_findings.md，格式必须是固定四列表格: summary、bug type、security rating、source files。
- security rating 只能使用 low、medium、high；不确定时先记录 unknown 风险描述但不要编造结论。
- 如果任务没有完成，不要只做泛泛总结；继续推进最有价值的审计路径。

目标补充说明:
{note or "无"}

用户消息:
{user_prompt}
"""
