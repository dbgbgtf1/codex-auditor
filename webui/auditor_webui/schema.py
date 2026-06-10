"""Shared typed structures for the WebUI."""

from __future__ import annotations

from typing import Final, TypedDict

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type RowValue = None | int | float | str
type RowDict = dict[str, RowValue]

BUSY_STATUSES: Final[frozenset[str]] = frozenset({"running", "judging"})
JUDGE_CONTINUE_MESSAGE: Final = "正在继续工作流"
JUDGE_STOP_MESSAGE: Final = "等待用户输入"


class JudgeResult(TypedDict):
    """Structured response from the small-model stop/continue judge."""

    continue_: bool
    reason: str
    report: str
    source: str


def row_str(row: RowDict, key: str, default: str = "") -> str:
    value = row.get(key)
    if value is None:
        return default
    return str(value)


def row_optional_str(row: RowDict, key: str) -> str | None:
    value = row.get(key)
    return None if value is None else str(value)


def row_int(row: RowDict, key: str, default: int = 0) -> int:
    value = row.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default
