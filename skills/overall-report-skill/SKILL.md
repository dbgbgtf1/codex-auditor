---
name: overall-report-skill
description: Guide Codex during binary security audits to maintain Chinese 标识符-000/OVERALL.md vulnerability summary rating reports and regenerate machine-readable overall.json from the Markdown table after every audit turn. Use when updating audit outputs, summarizing vulnerabilities, assigning report priority scores, or validating/exporting OVERALL.md.
---

# Overall Report Skill

用于维护 `/data/workspace/audit/<标识符>-000/OVERALL.md`。该文件是累计漏洞总结评级报告，每轮对话结束时都要更新，并在更新后重新生成同目录的 `overall.json`。

## 更新原则

1. 使用中文编写，保留历史已确认漏洞；新发现、复核、降级、确认无效的结论都要反映到最新内容中。
2. 只把达到报告标准、已经建立或准备建立 `<标识符>-xxx` 漏洞目录的条目放入漏洞表。误报、待验证线索、环境坑点写入 `REMINDER.md`，不要混入总表。
3. 每个漏洞编号必须形如 `<标识符>-001`，不要把 `<标识符>-000` 写成漏洞编号。
4. `评分` 是 0-100 的整数，表示需要立刻报送修复的优先级，不等同于 CVSS。
5. `EXP是否存在` 只能写 `是` 或 `否`。只有存在EXP，并能在普通构建或明确下游调用场景中触发时写 `是`。
6. 表格列名和顺序必须保持不变，便于脚本解析。字段中如必须出现竖线，写成 `\|`。

## OVERALL.md 模板

```markdown
# 漏洞总结评级报告

更新时间: 2026-05-22 18:30:00 +0800
目标标识符: LIBSSH

## 本轮更新

- 本轮新增/复核/降级/排除的核心结论。
- 如果暂无确认漏洞，说明当前主要分析进展和剩余风险。

## 漏洞总览

| 漏洞编号 | 漏洞类型 | 受影响模块 | 利用难度 | EXP是否存在 | 评分 | 概要 |
|---|---|---|---|---|---:|---|
| LIBSSH-001 | 堆溢出 | src/foo.c:parse_bar | 低 | 是 | 92 | 默认配置下可由公开入口稳定触发，存在控制堆元数据并进一步利用的机会。 |
```

没有确认漏洞时，保留表头和分隔行，不要添加“暂无”占位行。

## 字段口径

- `漏洞编号`: `<标识符>-NNN`，`NNN` 从 `001` 起，和单独漏洞目录保持一致。
- `漏洞类型`: 简短类型，如 `堆溢出`、`栈溢出`、`UAF`、`越界读`、`空指针崩溃`、`任意文件读`、`逻辑缺陷`。
- `受影响模块`: 写清模块、文件、函数或协议路径，优先使用 `path/to/file.c:function`。
- `利用难度`:
  - `低`: 可通过现成开源项目触达；可执行程序在默认配置下稳定触发，或库项目能被真实下游项目调用触发，且确实存在 RCE、越权、信息泄露前置等实际利用机会。
  - `中`: 需要修改默认配置但配置方式现实可见，或触发入口常见但利用链复杂、不稳定。
  - `高`: 代码路径冷门、调用方式牵强、需要不现实的使用方式，实际攻击面很弱。
- `EXP是否存在`: `是` 表示已有可运行复现材料且有明确证据能表示仅通过exp能揭示漏洞可利用；`否` 表示仍只有理论分析、崩溃样例不足、尚未整理成脚本或只有poc，针对目标是asan编译结果，且无法确认漏洞对真实环境有害。
- `评分`: 按“是否应立即报送修复”评分。90-100 为默认/常见路径上的高危内存破坏或明确 RCE 风险；70-89 为高影响但配置或利用条件较高；40-69 为稳定崩溃、任意读、明显安全边界问题；1-39 为低优先级 bug 或安全弱点；0 只用于保留但不建议报送的历史结论。
- `概要`: 一句话说明触发入口、影响和当前证据，不写长篇细节；细节放入对应 `<标识符>-xxx` 报告。

## 生成 overall.json

每次更新 `OVERALL.md` 后，必须运行本 skill 自带脚本重新生成 `overall.json`：

```bash
python3 scripts/overall_md_to_json.py /data/workspace/audit/<标识符>-000/OVERALL.md
```

如果从仓库路径调用，使用：

```bash
python3 /data/skills/overall-report-skill/scripts/overall_md_to_json.py /data/workspace/audit/<标识符>-000/OVERALL.md
```

脚本会默认把 JSON 写到 `OVERALL.md` 同目录的 `overall.json`。如果校验失败，先修正 Markdown 表格，再重新运行脚本；不要手工编辑 `overall.json`。
