"""Target workspace preparation and atomic file updates."""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path

from .config import CONFIG, TARGET_NAME_RE, target_workspace

NOTE_HEADING = "## 补充说明"


def validate_target_name(value: str) -> str:
    name = value
    if not TARGET_NAME_RE.fullmatch(name):
        raise ValueError("目标名必须以字母或数字开头，只能包含字母、数字、点、下划线和短横线，长度不超过 64")
    if name in {".", "..", "audit", "temp", "templates"}:
        raise ValueError("目标名与系统目录冲突")
    return name


def prepare_target_workspace(name: str, note: str) -> Path:
    workspace = target_workspace(validate_target_name(name))
    if workspace.exists():
        raise ValueError("目标工作区目录已存在")
    if not CONFIG.templates_dir.exists():
        raise FileNotFoundError(f"模板目录不存在: {CONFIG.templates_dir}")
    shutil.copytree(CONFIG.templates_dir, workspace)
    write_init_note(workspace, note)
    return workspace


def validate_workspace_for_delete(workspace: Path) -> Path:
    root = CONFIG.workspace.resolve()
    resolved = workspace.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError("目标工作区路径不在配置的 workspace 根目录内")
    return resolved


def delete_target_workspace(workspace: Path) -> None:
    resolved = validate_workspace_for_delete(workspace)
    if resolved.exists():
        if not resolved.is_dir():
            raise ValueError("目标工作区路径不是目录")
        shutil.rmtree(resolved)


def write_init_note(workspace: Path, note: str) -> None:
    init_path = workspace / "init.md"
    text = init_path.read_text(encoding="utf-8", errors="ignore") if init_path.exists() else f"{NOTE_HEADING}\n"
    updated = replace_note_section(text, note)
    atomic_write_text(init_path, updated)


def replace_note_section(text: str, note: str) -> str:
    lines = text.splitlines()
    heading_index = next((index for index, line in enumerate(lines) if line.strip() == NOTE_HEADING), None)
    note_lines = note.rstrip().splitlines()
    if heading_index is None:
        base = text.rstrip()
        prefix = f"{base}\n\n" if base else ""
        return f"{prefix}{NOTE_HEADING}\n{note.rstrip()}\n"

    end_index = len(lines)
    for index in range(heading_index + 1, len(lines)):
        line = lines[index]
        if line.startswith("## ") and line.strip() != NOTE_HEADING:
            end_index = index
            break

    replacement = [NOTE_HEADING, *note_lines]
    updated_lines = [*lines[:heading_index], *replacement, *lines[end_index:]]
    return "\n".join(updated_lines).rstrip() + "\n"


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(tmp_name)
