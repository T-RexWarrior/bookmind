# BookMind 开发、数据与迁移

产品介绍与快速开始见 [项目首页](../README.md)。本页记录本地开发、数据目录、旧版迁移与验证方式。

## 本地开发

当前应用从 `deeptutor.bookmind.app:app` 启动后端，启用 BookMind 的功能范围与教材关联接口。前端位于 `web/`，使用 Next.js。

推荐先按项目首页完成首次启动，建立 `.venv/`、前端依赖与本机设置。之后可以分别运行：

```powershell
# 在项目根目录启动后端
.\.venv\Scripts\python.exe -m uvicorn deeptutor.bookmind.app:app --host 127.0.0.1 --port 8019

# 另一个 PowerShell 窗口：在项目根目录启动前端开发服务
cd web
npm.cmd run dev -- --hostname 127.0.0.1 --port 8018
```

先通过 `stop.ps1` 停止已经运行的正式服务，避免占用同一端口。通过启动脚本运行正式构建：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 -NoBrowser
powershell -ExecutionPolicy Bypass -File .\start.ps1 -Rebuild
powershell -ExecutionPolicy Bypass -File .\stop.ps1
```

## 数据目录

| 位置 | 内容 |
| --- | --- |
| `data/user/workspace/reading/` | 资料、原文、标注、阅读位置与教材关联数据库 |
| `data/user/workspace/notebook/` | 学习笔记 |
| `data/user/` 下的原版存储目录 | `LearningStore`、`SqliteSessionStore` 等保存的路径、会话与学习记录 |
| `data/user/settings/` | 本机模型与界面设置，包含 API 密钥 |
| `.venv/`、`web/node_modules/`、`.cache/` | 本机依赖与下载缓存 |

上述运行数据和依赖均由 `.gitignore` 排除。公开演示截图位于 `docs/images/`，仅展示专用演示资料；浏览器联调产生的完整截图、响应与报告留在忽略提交的 `data/` 中。

## 从旧版迁移

初次启动时，若同级 `BookMind-v2/data/studytutor.db` 存在，启动脚本会运行 `scripts/migrate_v2.py`。脚本只读旧数据库，复制资料与笔记，保留旧应用的数据。

旧版练习不会仅凭名称猜测对应新知识点，也不会自动转换为新的掌握证据。迁移后继续使用 Reading、Mastery Path、Notebook 和 Assessment 的原有存储与评估机制。

内部仍保留 `deeptutor` 包名、导入路径、环境变量与部分存储键。调整这些标识需要同步核对依赖和数据迁移，不能只替换显示名称。

## 模块与会话

- `deeptutor/bookmind/service.py` 建立 `material_id / workspace_id / path_id / notebook_id` 的稳定关联。
- `deeptutor/bookmind/practice.py` 绑定专项练习的教材、知识点、原文范围与生成轮次。
- `deeptutor/bookmind/generated.py` 将完整 Books 结果导入为可学习的资料快照。
- `deeptutor/bookmind/session_scope.py` 在请求进入执行流程前核对会话归属，阻止阅读、导学与专项练习相互改用会话。
- 阅读前端先验证会话属于当前教材，再加载聊天记录。旧的串号地址恢复到本书阅读对话；切换路由会取消过时的加载，不取消已在后台执行的模型回复。

更多关联说明见 [原版模块融合说明](原版模块融合说明.md)、[AI 导学改版说明](AI导学改版说明.md) 和 [目标学习说明](目标学习说明.md)。

## 自动化验证

在项目根目录执行后端测试：

```powershell
.\.venv\Scripts\python.exe -m pip install pytest pytest-asyncio
.\.venv\Scripts\python.exe -m pytest -q tests/test_bookmind_native.py tests/test_bookmind_diagrams_outline.py deeptutor/learning/tests/test_mastery_state_integrity.py deeptutor/learning/tests/test_mastery_choices.py deeptutor/learning/tests/test_scheduler.py
```

在 `web/` 中执行前端检查：

```powershell
npm.cmd run typecheck
npm.cmd run build
npm.cmd run test:unit -- tests/inline-study-diagram.spec.ts tests/reading-session-isolation.spec.tsx tests/mastery-question-card.spec.tsx tests/mastery-question-group.spec.tsx tests/notebook-record-edit-discoverability.spec.tsx tests/reading-quiz-persistence.spec.tsx
```

当前相关验证结果为后端 **145 项**、相关前端 **35 项**通过；原文引用另有 **42 项**检查通过，TypeScript 检查和正式构建通过。测试数量会随版本变化。

## 浏览器联调

服务已启动、并准备好脚本指定的专用演示资料后，在 `web/` 中执行：

```powershell
node scripts/check-bookmind.mjs
node scripts/check-bookmind-modes.mjs
node scripts/check-bookmind-goals.mjs
node scripts/check-bookmind-session-isolation.mjs
node scripts/check-bookmind-diagrams.mjs
```

脚本当前按本机 Chrome 路径启动浏览器；其他环境需要调整 `executablePath`。模式、目标生成和会话隔离脚本提供 `--live`，会使用已配置模型发出真实请求。仅在专用演示教材上运行，避免把联调作答写进实际学习记录。

会话隔离联调覆盖练习切回阅读、导学切回阅读、重复页面切换、旧错误地址恢复，以及实际发送消息后的会话归属。联调报告和截图保存在 `data/`，不随源码提交。

图解联调加 `--live` 会在专用演示教材中发送“画个二叉树的示意图我看一下”，检查实际 SVG、刷新恢复与手机布局。已有演示会话可用 `--resume-session=<会话 ID>` 复查显示，不再调用模型。该脚本也只读核对本机已有示例 PDF 的目录清理结果，不重写资料。
