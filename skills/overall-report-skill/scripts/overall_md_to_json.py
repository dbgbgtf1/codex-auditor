#!/usr/bin/env python3
"""Convert a Chinese audit OVERALL.md vulnerability table to overall.json."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


REQUIRED_HEADERS = [
    "漏洞编号",
    "漏洞类型",
    "受影响模块",
    "利用难度",
    "EXP是否存在",
    "评分",
    "概要",
]

HEADER_TO_KEY = {
    "漏洞编号": "vulnerability_id",
    "漏洞类型": "vulnerability_type",
    "受影响模块": "affected_module",
    "利用难度": "exploit_difficulty",
    "EXP是否存在": "exp_exists",
    "评分": "score",
    "概要": "summary",
}

DIFFICULTY_VALUES = {"低", "中", "高"}
EXP_VALUES = {"是": True, "否": False}
VULN_ID_RE = re.compile(r"^([A-Z][A-Z0-9_]*)-(\d{3})$")


class OverallParseError(ValueError):
    """Raised when OVERALL.md cannot be converted safely."""


@dataclass(frozen=True)
class MarkdownTable:
    header_line: int
    rows: list[tuple[int, list[str]]]


def _has_unescaped_trailing_pipe(text: str) -> bool:
    if not text.endswith("|"):
        return False
    backslashes = 0
    idx = len(text) - 2
    while idx >= 0 and text[idx] == "\\":
        backslashes += 1
        idx -= 1
    return backslashes % 2 == 0


def split_markdown_row(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if _has_unescaped_trailing_pipe(text):
        text = text[:-1]

    cells: list[str] = []
    buf: list[str] = []
    escaped = False
    for ch in text:
        if escaped:
            if ch == "|":
                buf.append("|")
            else:
                buf.append("\\")
                buf.append(ch)
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "|":
            cells.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if escaped:
        buf.append("\\")
    cells.append("".join(buf).strip())
    return cells


def is_separator_row(cells: Iterable[str]) -> bool:
    return all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


def find_overall_table(lines: list[str]) -> MarkdownTable:
    saw_near_miss = False
    for index, line in enumerate(lines):
        if "|" not in line:
            continue
        cells = split_markdown_row(line)
        if cells != REQUIRED_HEADERS:
            if any(header in cells for header in REQUIRED_HEADERS):
                saw_near_miss = True
            continue

        separator_index = index + 1
        if separator_index >= len(lines):
            raise OverallParseError(f"第 {index + 1} 行表头后缺少 Markdown 分隔行")
        separator_cells = split_markdown_row(lines[separator_index])
        if len(separator_cells) != len(REQUIRED_HEADERS) or not is_separator_row(separator_cells):
            raise OverallParseError(f"第 {separator_index + 1} 行不是合法的 Markdown 表格分隔行")

        rows: list[tuple[int, list[str]]] = []
        for row_index in range(separator_index + 1, len(lines)):
            row_line = lines[row_index]
            if not row_line.strip():
                break
            if "|" not in row_line:
                break
            row_cells = split_markdown_row(row_line)
            if len(row_cells) != len(REQUIRED_HEADERS):
                raise OverallParseError(
                    f"第 {row_index + 1} 行列数为 {len(row_cells)}，应为 {len(REQUIRED_HEADERS)}"
                )
            if all(not cell for cell in row_cells):
                continue
            rows.append((row_index + 1, row_cells))
        return MarkdownTable(header_line=index + 1, rows=rows)

    if saw_near_miss:
        raise OverallParseError("找到了疑似漏洞表，但列名或顺序不符合要求")
    raise OverallParseError("未找到 OVERALL.md 漏洞总览表")


def detect_identifier(md_path: Path) -> str | None:
    parent_name = md_path.parent.name
    match = re.fullmatch(r"([A-Z][A-Z0-9_]*)-000", parent_name)
    if match:
        return match.group(1)
    return None


def normalize_cell(value: str) -> str:
    return " ".join(value.strip().split())


def validate_and_convert(
    table: MarkdownTable,
    *,
    identifier: str | None,
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    inferred_identifier: str | None = identifier

    for line_number, cells in table.rows:
        row = dict(zip(REQUIRED_HEADERS, (normalize_cell(cell) for cell in cells), strict=True))

        for header, value in row.items():
            if not value:
                raise OverallParseError(f"第 {line_number} 行 `{header}` 不能为空")

        vuln_id = row["漏洞编号"]
        match = VULN_ID_RE.fullmatch(vuln_id)
        if not match:
            raise OverallParseError(f"第 {line_number} 行漏洞编号 `{vuln_id}` 不符合 `<标识符>-NNN`")

        row_identifier, number_text = match.groups()
        if number_text == "000":
            raise OverallParseError(f"第 {line_number} 行不能使用 `{row_identifier}-000` 作为漏洞编号")
        if inferred_identifier is None:
            inferred_identifier = row_identifier
        if row_identifier != inferred_identifier:
            raise OverallParseError(
                f"第 {line_number} 行漏洞编号前缀 `{row_identifier}` 与标识符 `{inferred_identifier}` 不一致"
            )
        if vuln_id in seen_ids:
            raise OverallParseError(f"第 {line_number} 行漏洞编号 `{vuln_id}` 重复")
        seen_ids.add(vuln_id)

        difficulty = row["利用难度"]
        if difficulty not in DIFFICULTY_VALUES:
            allowed = "、".join(sorted(DIFFICULTY_VALUES))
            raise OverallParseError(f"第 {line_number} 行利用难度 `{difficulty}` 非法，只能为 {allowed}")

        exp_text = row["EXP是否存在"]
        if exp_text not in EXP_VALUES:
            raise OverallParseError(f"第 {line_number} 行 EXP是否存在 `{exp_text}` 非法，只能为 是 或 否")

        score_text = row["评分"]
        if not re.fullmatch(r"\d{1,3}", score_text):
            raise OverallParseError(f"第 {line_number} 行评分 `{score_text}` 不是 0-100 的整数")
        score = int(score_text)
        if score > 100:
            raise OverallParseError(f"第 {line_number} 行评分 `{score_text}` 超出 0-100")

        findings.append(
            {
                HEADER_TO_KEY["漏洞编号"]: vuln_id,
                HEADER_TO_KEY["漏洞类型"]: row["漏洞类型"],
                HEADER_TO_KEY["受影响模块"]: row["受影响模块"],
                HEADER_TO_KEY["利用难度"]: difficulty,
                HEADER_TO_KEY["EXP是否存在"]: EXP_VALUES[exp_text],
                "exp_exists_text": exp_text,
                HEADER_TO_KEY["评分"]: score,
                HEADER_TO_KEY["概要"]: row["概要"],
                "source_line": line_number,
            }
        )

    return findings


def build_payload(md_path: Path, identifier: str | None, findings: list[dict[str, object]]) -> dict[str, object]:
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if identifier is None and findings:
        first_id = str(findings[0]["vulnerability_id"])
        identifier = first_id.rsplit("-", 1)[0]
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "source": str(md_path),
        "identifier": identifier,
        "count": len(findings),
        "findings": findings,
    }


def convert_file(md_path: Path, output_path: Path, identifier: str | None) -> dict[str, object]:
    if not md_path.exists():
        raise OverallParseError(f"输入文件不存在: {md_path}")
    if not md_path.is_file():
        raise OverallParseError(f"输入路径不是文件: {md_path}")

    text = md_path.read_text(encoding="utf-8")
    table = find_overall_table(text.splitlines())
    effective_identifier = identifier or detect_identifier(md_path)
    findings = validate_and_convert(table, identifier=effective_identifier)
    payload = build_payload(md_path, effective_identifier, findings)

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert 标识符-000/OVERALL.md vulnerability table to overall.json with validation."
    )
    parser.add_argument("overall_md", type=Path, help="Path to OVERALL.md")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output JSON path, default: overall.json next to OVERALL.md",
    )
    parser.add_argument(
        "--identifier",
        help="Expected identifier prefix, e.g. LIBSSH. Defaults to parent directory prefix when named LIBSSH-000.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    md_path = args.overall_md.resolve()
    output_path = args.output.resolve() if args.output else md_path.with_name("overall.json")

    if args.identifier is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]*", args.identifier):
        print(f"error: --identifier `{args.identifier}` 必须匹配 [A-Z][A-Z0-9_]*", file=sys.stderr)
        return 2

    try:
        payload = convert_file(md_path, output_path, args.identifier)
    except OverallParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"wrote {output_path} ({payload['count']} findings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
