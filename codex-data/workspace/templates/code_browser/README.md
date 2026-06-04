# 目标无关代码浏览器

这是一个本地 SQLite 索引和 CLI，用于收集机械审计事实。它不是 Web UI，也不判断根因。

构建索引：

```sh
python3 code_browser/build_index.py \
  --workspace /目标/路径 \
  --db /目标/路径/.audit/code_browser.sqlite \
  --path src \
  --path include \
  --path tests \
  --route-file /目标/路径/code_browser/routes.json \
  --max-commits 500
```

`routes.json` 是可选路由文件，格式如下：

```json
[
  {
    "pattern": "src/parser",
    "skill": "target-parser-audit",
    "reason": "输入格式解析、字段边界和错误恢复"
  }
]
```

查询示例：

```sh
python3 code_browser/query.py --workspace /目标/路径 meta
python3 code_browser/query.py --workspace /目标/路径 context SomeSymbol
python3 code_browser/query.py --workspace /目标/路径 context src/foo.c:120-150
python3 code_browser/query.py --workspace /目标/路径 route src/parser/read.c
python3 code_browser/query.py --workspace /目标/路径 refs parse_header --limit 20
python3 code_browser/query.py --workspace /目标/路径 tests parse_header --limit 20
python3 code_browser/query.py --workspace /目标/路径 commits parser --limit 20
```

索引保存路径、轻量符号、可选文本引用、测试、近期 commit、路由提示和 git 元数据。当这个索引过粗时，继续使用 `rg`、语言服务器、调试器或项目专用工具。
