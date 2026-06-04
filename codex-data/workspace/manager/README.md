# Manager

本地挖掘控制器，只做两件事：

- 从目标列表启动 mining agent。
- 扫描 `/data/workspace/{target}/archives/{id}-{description}/report.md`，复制到 `/data/workspace/audit/{target}-{id}-{description}.md`。

启动新目标 agent 前会执行：

```sh
cp /data/workspace/templates/ -r /data/workspace/{target}
```

该 agent 的 workspace 为 `/data/workspace/{target}`。

已存在的 audit 文件不会重复计入新增。

## 用法

启动持续挖掘：

```sh
python3 manager/manager.py mining targets.txt --limit 3
```

运行单次 tick：

```sh
python3 manager/manager.py tick targets.txt --limit 3
```

`targets.txt` 每行一个目标，可以带编号：

```text
1. libssh
2. libpng
3. openssh
```

每次 tick 会输出：

```text
running ["libssh", "libpng"], starting ["openssh"]
libssh: { low: 3, medium: 1, high: 2 }, libssh_new: { low: 1, medium: 0, high: 0 }
libpng: { low: 3, medium: 1, high: 2 }, libpng_new: { low: 1, medium: 0, high: 0 }
openssh: { low: 3, medium: 1, high: 2 }, openssh_new: { low: 1, medium: 0, high: 0 }
```

## 配置

默认配置写入 `manager/config.json`：

```json
{
  "workspace_root": "/data/workspace",
  "audit_root": "/data/workspace/audit",
  "state_dir": "manager/state",
  "task_timeout_sec": 14400
}
```
