# AGENTS.md

## 注意事项

1. 每次改动之后都必须创建一个对应的 GIT COMMIT，以便后续追踪和回滚。
2. 每次改动之后都必须编写和更新相关测试，并在交付给用户前，确保所有验证和测试都通过。

## 本项目怎么做到上面两条

提交前依次跑完，全部通过才算交付：

```powershell
.\.venv\Scripts\python.exe -m pytest -q        # 后端测试
.\.venv\Scripts\python.exe -m ruff check .     # 静态检查
.\.venv\Scripts\python.exe -m ruff format --check .
cd frontend; npm run build                     # 前端构建（改了前端才需要）
```

- 改了模型字段，必须同时补一条 Alembic 迁移，并在本地执行
  `.\.venv\Scripts\python.exe main.py migrate` 让 `tests/test_migrations.py` 的
  模型与迁移一致性检查通过。
- 修 bug 时先写一条能复现的测试，再改代码。
- 改动涉及前端时，要用浏览器实际操作验证一遍（本地服务：
  `.\scripts\start-local.ps1 -Background`，界面在 http://127.0.0.1:8000 ）。

## 本项目的行为约定（踩过的坑）

- **运行时不热加载线路。** 新建/删除线路、换监听源、改业务类型（A↔B）之后，
  必须到「运行总览」点「重启」才生效；改配置、增删接收目标、启停线路是立即生效的。
  运行总览会在有线路没被接管时给出黄条提醒。
- **一条线路可以多选监听源。** 落库时按「一个源一条线路」拆开，共用 `routes.bundle_id`，
  水位线（`route_target_progress`）是按「线路 + 目标」记的，所以不能合并成一行。
- **线索是业务数据。** `leads` 的外键是 `ON DELETE SET NULL`，删线路/删群不能连带删除线索。
- **B 线默认只入库不刷屏。** 全量监听时只有命中关键词（或打开「全量模式也推卡片」）
  才会推卡片到接收群，其余都在「线索池」页查看。
