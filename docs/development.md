# 安装与开发

产品功能见[项目首页](../README.md)，模块职责见[架构说明](architecture.md)。

## 启动

需要 Python 3.11–3.14、Node.js 22+。Windows 首次运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

启动脚本在项目目录安装 `.venv/`、前端依赖与缓存，初始化本机设置，构建前端，然后启动 API（8019）和页面（8018）。在页面「API 设置」中配置支持工具调用的文字模型。

```powershell
# 修改前端后重新构建并启动
powershell -ExecutionPolicy Bypass -File .\stop.ps1
powershell -ExecutionPolicy Bypass -File .\start.ps1 -Rebuild -NoBrowser

# 停止本项目进程
powershell -ExecutionPolicy Bypass -File .\stop.ps1
```

开发时，在根目录安装后端依赖，分别启动前后端：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe scripts/setup_native.py
.\.venv\Scripts\python.exe -m uvicorn bookmind.main:app --host 127.0.0.1 --port 8019
```

```powershell
cd web
npm.cmd ci
npm.cmd run dev -- --hostname 127.0.0.1 --port 8018
```

手动部署可以使用相同的 Python / npm 命令；一键脚本针对 Windows，其他平台需要自行管理启动进程。BookMind 目前从本仓库安装，不使用公共 PyPI 同名包更新，也不从上游项目自动覆盖源码。

## 配置与数据

`BOOKMIND_HOME` 指向运行目录，默认是当前工作目录。运行数据统一放在 `<运行目录>/data/`，源码改名不会改变这份数据的位置。

| 位置 | 内容 |
| --- | --- |
| `data/user/workspace/reading/` | 资料、原文、目录、标注、阅读位置和 `bookmind.sqlite` 教材关联 |
| `data/user/workspace/notebook/` | 笔记与引用 |
| `data/user/` 下原生存储 | 会话、学习路径、评估、练习与复习状态 |
| `data/user/settings/` | 模型与界面设置，包含本机 API 密钥 |
| `.venv/`、`web/node_modules/`、`.cache/` | 依赖与下载缓存 |

运行数据、密钥、日志和本机依赖由 `.gitignore` 排除。公开截图仅使用演示资料。

## 改名与旧版兼容

内部包名为 `bookmind`，产品关联层位于 `bookmind/textbook/`，ASGI 入口为 `bookmind.main:app`，CLI 为 `bookmind` / `python -m bookmind_cli`。

- 已有虚拟环境需重新执行 `python -m pip install --no-deps -e .`，更新包元数据和命令入口；依赖未齐时使用完整的 `pip install -e .`。
- 旧的 `DEEPTUTOR_*` 环境变量由后端 `compat.py` 与前端 `brand-environment.cjs` 兼容读取。显式设置的 `BOOKMIND_*` 优先。
- 首次打开新版页面时，主题、语言、会话选择与草稿等旧浏览器存储键复制到新命名空间，已有新值优先。IndexedDB 草稿和导入记录同样复制并记账，旧数据库保留。
- 不改写教材 ID、知识点 ID、历史对话文本、图解记录或数据库文件名；已有引用和练习关联继续有效。
- 上游项目名称仅保留在来源声明、历史问题链接、真实第三方服务地址及兼容边界中。

初次初始化时，同级 `BookMind-v2/data/studytutor.db` 若存在，会通过 `scripts/migrate_v2.py` 只读迁移资料与笔记。旧练习不凭名称猜测新知识点，不自动当作新掌握证据。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pip install pytest pytest-asyncio
.\.venv\Scripts\python.exe -m pytest -q tests/test_bookmind_native.py tests/test_bookmind_diagrams_outline.py tests/test_bookmind_brand_migration.py bookmind/learning/tests/test_mastery_state_integrity.py bookmind/learning/tests/test_mastery_choices.py bookmind/learning/tests/test_scheduler.py
.\.venv\Scripts\python.exe -m bookmind.api.contracts.export --check
```

在 `web/` 中：

```powershell
$env:NEXT_TELEMETRY_DISABLED='1'
npm.cmd run contracts:check
npm.cmd run typecheck
npm.cmd run build
npm.cmd run test:unit -- tests/svg-text-alignment.spec.ts tests/inline-study-diagram.spec.ts tests/reading-session-isolation.spec.tsx tests/mastery-question-card.spec.tsx tests/mastery-question-group.spec.tsx tests/notebook-record-edit-discoverability.spec.tsx tests/reading-quiz-persistence.spec.tsx
node scripts/check-bookmind-brand-migration.mjs
```

接口改变后，先导出后端契约，再运行 `npm.cmd run contracts:generate`。已准备专用演示教材的本机可继续运行 `check-bookmind-modes.mjs`、`check-bookmind-goals.mjs`、`check-bookmind-session-isolation.mjs` 做界面回归。联调脚本的真实模型调用使用 `--live` 显式开启，输出保存在忽略提交的 `data/` 中。
