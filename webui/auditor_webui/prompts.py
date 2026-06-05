"""Prompt templates used by the automation worker."""

from __future__ import annotations

from .config import CONFIG
from .schema import RowDict, row_str


def base_prompt(session: RowDict, user_prompt: str, *, first_turn: bool) -> str:
    identifier = row_str(session, "identifier")
    heading = "这是该 WebUI 会话的首次请求。" if first_turn else "这是该 WebUI 会话的后续请求。"
    audit_prefix = CONFIG.audit_dir / f"{identifier}-xxx"
    summary_dir = CONFIG.audit_dir / f"{identifier}-000"
    return f"""你正在执行 codex-auditor 自动化二进制安全审计会话。{heading}

目标标识符: {identifier}

必须遵守:
- 阅读并遵守 {CONFIG.agents_path}。
- 本 WebUI 当前配置的工作目录、审计目录优先级高于环境文档中的默认路径。
- 所有审计报告写入 {audit_prefix}。
- 每轮结束前维护 {summary_dir}/COVERAGE.md、OVERALL.md、REMINDER.md。
- 更新 OVERALL.md 后根据 {CONFIG.skills_dir}/overall-report-skill/SKILL.md 生成 overall.json。
- 如果任务没有完成，不要只做泛泛总结；继续推进最有价值的审计路径。

用户消息:
{user_prompt}
"""


def auto_continue_prompt(reason: str) -> str:
    return f"""停顿判断器认为当前不需要等待用户，原因: {reason}

请继续当前审计任务，优先推进尚未完成的路径；
结束前继续维护标识符-000 下的 COVERAGE.md、OVERALL.md、REMINDER.md，并确保 overall.json 已更新。"""
