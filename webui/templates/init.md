## 任务目标

总目标为审计用户给定的目标，找到有价值的二进制漏洞，不需要进行利用侧的研究。如果该目录下以下工作尚未完成，说明你是第一个agent，你需要为后续agent配置好工作环境。你的checklist如下。如果以下任务已完成，阅读补充说明后使用 $bug-hunting 继续完成任务

1. 拉取目标程序完整源码并编译，要求asan和release和debug三个版本，配置好尽可能可用的调试环境。
2. 将该目标常见高危的模块和漏洞路径整理出具体文件和目录，写到 ./vuln.md , 方便后续agent进行针对审计。
3. 配置好 code_browser ，确保 pytest 通过和高可用性。

如遇到无法解决的问题(如网络配置出错, 不知道密码而无法获得权限继续, 在 /data/workspace/mining.md 下取消登记 {target}，并说明原因

## 目录结构与协议

- `code_browser/`：目标无关源码索引和查询 CLI
- `verify/`：PoC、输入文件、harness 和目标二进制的命令矩阵验证器
- `report_template/`：候选漏洞产物模板
- `archives/known_findings.md`: 整理的全部发现集合
- `archives/known_fails.md`: 整理的全部失败集合
- `archives/{id}-{description}`: 候选漏洞产物目录
- `$bug-confirming`: 候选漏洞落地指南
- `$bug-hunting`: 挖掘漏洞指导
- `init.md`: 初始化指南
- `vuln.md`: 记录的攻击面和后续的审计结果

## 补充说明
