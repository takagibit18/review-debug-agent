# Error Log

记录开发过程中的错误与对应解决方式，按时间倒序追加。

| Date | Module | Error | Cause | Fix |
|------|--------|-------|-------|-----|
| 2026-04-15 | `eval` / golden 标注 | LLM 草稿把不适合作「缺陷检出」的 diff（如纯加功能、常规扩展）标成 `critical`/`warning` 等 expected issue，造成假阳性黄金标签 | 生成侧 `min_expected_issues≥1` 与标注器「无可靠缺陷可返回空」并存，易逼出凑数 issue；bugfix PR 筛选与「可证伪缺陷」未强绑定；人工复核未完成即以 `expected` 为准 | 计划不保留本批 PR 样本；后续策略见团队方案（本条目仅记录现象，实施另跟踪） |
| 2026-04-15 | `eval` / review pipeline | Golden `hit_rate`≈0；有时为占位 summary 且 `issues=[]` | `submit_review` 与 `ReviewReport` 校验失败被静默丢弃；prompt 弱；`location_pattern` 按子串匹配与正则 fixture 不符；未复核样本 severity 门槛过严 | `submit_review` 增加 severity 枚举；加强 review prompt；`InferenceEngine` severity 映射 + 校验失败打日志；`_match_issues` 改为正则匹配，未复核 fixture 在严格匹配失败时宽松配对 |
| 2026-04-09 | `scripts/smoke_test_models.py` | Ruff `E402` (import not at top) | 为了解决路径问题把 `src` 导入放在路径注入后的模块级位置，违反 lint 规则 | 改为在 `run_smoke_test()` 内局部导入 |
| 2026-04-09 | `scripts/smoke_test_models.py` | `ModuleNotFoundError: No module named 'src'` | 直接运行脚本时，项目根目录未保证进入 `sys.path` | 在脚本中注入项目根路径到 `sys.path` 后再导入 `src.*` |
| 2026-04-09 | `src/models/client.py` | Ruff `F841` x3 | `except ... as exc` 捕获了异常变量但未使用 | 去掉未使用的 `as exc` |
| 2026-04-09 | smoke test command | `SyntaxError` when using `python -c` with `async def` | `python -c` 单行不适合定义复合 `async` 语句 | 改用独立脚本 `scripts/smoke_test_models.py` 并用 `asyncio.run()` 启动 |
