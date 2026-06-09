# manager.py 协议与已知问题

`manager.py` 是二进制审计流水线的本地调度器。它读取目标列表，为每个目标启动一个 Codex mining agent，并把各目标 `archives/*/report.md` 汇总到 audit 目录。

## 命令协议

```sh
python3 manager/manager.py [--config manager/config.json] mining TARGET_FILE
python3 manager/manager.py [--config manager/config.json] collect TARGET_FILE
python3 manager/manager.py [--config manager/config.json] reset
```

- `mining`：每 60 秒读取一次 `TARGET_FILE`，回收已结束的 mining 任务，按并发上限启动新任务，然后扫描归档报告。
- `collect`：只扫描一次归档报告，不启动 agent。
- `reset`：清空 `audit_root` 和 `state_dir` 目录内容；目录不存在时会创建空目录。
- `--config`：JSON 配置路径。默认是 `manager/config.json`；文件不存在时会写入默认配置。

## 配置协议

默认配置：

```json
{
  "workspace_root": "/data/workspace",
  "audit_root": "/data/workspace/audit",
  "state_dir": "manager/state",
  "task_timeout_sec": 14400,
  "codex_model": "gpt-5.5",
  "codex_command": "codex exec --model {model} -C {workspace} --dangerously-bypass-approvals-and-sandbox -"
}
```

- `workspace_root`：目标工作区根目录。相对路径会按仓库根目录解析。
- `audit_root`：汇总报告输出目录。相对路径会按仓库根目录解析。
- `state_dir`：SQLite 队列和运行日志目录。相对路径会按仓库根目录解析。
- `task_timeout_sec`：running 任务超时时间，默认 4 小时。
- `codex_model`：填入 `{model}` 的模型名。
- `codex_command`：启动 agent 的 shell 命令模板，必须包含可选占位符 `{model}`、`{workspace}`。占位符值会先经过 `shlex.quote()`。

## TARGET_FILE 协议

每行一个 target 名称，可选在 target 后用括号写补充说明：

```text
1. nginx(重点关注 HTTP/2、chunked parser 和历史 CVE 相邻代码)
```

读取时会：

- 去掉 Markdown 序号或项目符号前缀，例如 `1. foo`、`- foo`、`* foo`。
- 如果行尾匹配 `target(补充说明)` 或 `target（补充说明）`，target 名称取括号前内容，括号内内容作为补充说明。
- 忽略空行和以 `#` 开头的注释行。
- 保留第一次出现的 target，后续重复项忽略。

当前代码没有限制 target 只能是简单目录名；见“已知问题”。

## 工作区协议

对 target `foo`，manager 认为目录结构如下：

```text
{workspace_root}/
  templates/
  foo/
    init.md
    bugs.md
    confirm.md
    code_browser/
    verify/
    report_template/
    archives/
      {id}-{description}/
        report.md
```

启动 mining 任务前会先检查 `{workspace_root}/{target}` 是否存在。若已存在，则直接复用该目录；若不存在，才执行：

```sh
cp {workspace_root}/templates/ -r {workspace_root}/{target}
```

如果 target 文件中带有补充说明，manager 会在启动 agent 前把说明追加到 `{workspace_root}/{target}/init.md` 末尾：

```markdown
## 补充说明

重点关注 HTTP/2、chunked parser 和历史 CVE 相邻代码
```

若相同说明已存在于 `init.md`，不会重复追加。

然后在 `{workspace_root}/{target}` 下运行 `codex_command`，stdin 为生成的 prompt：

```text
按照 init.md 完成开源二进制漏洞挖掘任务。目标为 {target}
```

## 归档收集协议

扫描位置：

```text
{workspace_root}/{target}/archives/*/report.md
```

输出位置：

```text
{audit_root}/{target}-{archive_dir_name}.md
```

如果输出文件已存在，manager 不会覆盖，也不会把该报告计入本轮 `new` 统计。

严重性统计只识别 `report.md` 中包含以下标记的行：

- `security relevance`
- `安全相关`

并在同一行用单词边界匹配：

- `high`
- `medium`
- `low`

## SQLite 队列协议

数据库路径：

```text
{state_dir}/queue.sqlite
```

表结构：

```sql
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
```

当前实际使用的任务字段：

- `type`：目前只使用 `mining`。
- `status`：`queued`、`running`、`done`、`failed`。
- `scope`：target 名称。
- `payload`：`{"scope": target, "target": target}`。
- `run_dir`：相对仓库根目录的运行目录。
- `pid`：启动的 `/bin/sh run.sh` 进程 pid，同时也是新 session 的进程组 id。
- `exit_code`：agent 退出码；超时写 `124`；进程消失但没有退出码文件写 `255`。
- `last_error`：非零退出码时写 `runner exit code N`。

状态流：

```text
queued -> running -> done
queued -> running -> failed
```

注意：当前实现会插入 `queued` 后立即调用 `start_task()`，没有独立消费历史 `queued` 任务的逻辑。

## 运行目录协议

每个任务的运行目录：

```text
{state_dir}/runs/task-{id:06d}/
```

产物：

- `prompt.md`：传给 agent 的 prompt。
- `run.sh`：实际执行脚本。
- `stdout.log`：agent stdout。
- `stderr.log`：agent stderr。
- `exit_code`：`run.sh` 写入的退出码。

`run.sh` 形态：

```sh
{codex_command} < prompt.md > stdout.log 2> stderr.log
printf '%s\n' "$?" > exit_code
```
