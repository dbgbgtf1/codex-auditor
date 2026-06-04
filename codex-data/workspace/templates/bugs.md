## 审计循环

1. 检查目标源码、测试、构建文件、发布历史和已知公告。
   - 优先用 `code_browser/query.py` 做机械查询：`meta`、`context`、`file`、`route`、`symbol`、`refs`、`tests`、`commits`。
   - 当 code browser stale、不可用或过粗时，fallback 到 `rg`、语言服务器、调试器或项目专用工具
   - 在 ./archives/known_fails.json 和 ./archives/known_findings.json 和 ./vuln.md 中读取前几轮agent的成果进行参考和去重。
2. 选择一个小漏洞假设。
   - 跟踪不可信输入或状态来源、边界条件、状态改变、危险使用点和复现判定方式。
   - 考虑同一源码点附近的相邻值和状态。
   - 公开漏洞用于生成变体方向，不做一比一复刻。
3. 写 PoC 前先建模。
   - 先读相关源码和测试。
   - 写输入或 harness 前，先定义预期失败模式和控制项。
   - 如果行为与模型不同，调查是路径没有触发、模型错误，还是目标确实暴露了真实分歧。
4. 停止、记录或打包。
   - 对不稳定、重复、符合预期、超出范围或不支持的发现，不创建候选产物。
   - 有用的负面证据追加到 ./archives/known_fails.json
   - ./vuln.md中记录哪些模块和文件被怎样审计过
   - 稳定、有源码支撑、可由目标二进制或 harness 复现、非重复的候选按照 confirm.md 进行落地
