# Error Log

记录开发过程中的错误与对应解决方式，按时间倒序追加。

| Date | Module | Error | Cause | Fix |
|------|--------|-------|-------|-----|
| 2026-04-09 | `scripts/smoke_test_models.py` | Ruff `E402` (import not at top) | 为了解决路径问题把 `src` 导入放在路径注入后的模块级位置，违反 lint 规则 | 改为在 `run_smoke_test()` 内局部导入 |
| 2026-04-09 | `scripts/smoke_test_models.py` | `ModuleNotFoundError: No module named 'src'` | 直接运行脚本时，项目根目录未保证进入 `sys.path` | 在脚本中注入项目根路径到 `sys.path` 后再导入 `src.*` |
| 2026-04-09 | `src/models/client.py` | Ruff `F841` x3 | `except ... as exc` 捕获了异常变量但未使用 | 去掉未使用的 `as exc` |
| 2026-04-09 | smoke test command | `SyntaxError` when using `python -c` with `async def` | `python -c` 单行不适合定义复合 `async` 语句 | 改用独立脚本 `scripts/smoke_test_models.py` 并用 `asyncio.run()` 启动 |
