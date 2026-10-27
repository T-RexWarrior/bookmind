# BookMind · 学迹

一个围绕**一份资料或一个学习目标**展开的 AI 学习助手。当前为可以本地运行的初版，界面与业务层重新编写，参考 DeepTutor 的阅读上下文、资料溯源、练习与笔记思路。

## 直接使用

双击项目中的 **`启动BookMind.cmd`**，浏览器会打开 **http://127.0.0.1:8018**。

启动脚本会将 Python 环境放在 `.venv`，前端依赖放在 `frontend/node_modules`，下载缓存在 `.cache`。首次启动需要安装 Python 3.11+ 和 Node 22+，并联网安装依赖。

首次使用时上传自己的资料，或填写一个目标生成学习手册。仓库提供 [二叉树示例资料](frontend/public/demo.md)，上传后即可体验阅读、问答、图解、练习和笔记。

进入左下角 **API 设置**，填写 Base URL、Key 和模型 ID，先测试连接，再保存。页面只显示“已保存密钥”，不会回显密钥。GitHub 仓库不包含密钥、学习记录、上传教材和本机依赖。

这是 BookMind 围绕资料学习重新实现的初版，当前版本替换旧版应用代码，旧版仍可从 Git 提交历史查看。

## 已实现的功能

| 页面 | 可以做什么 | 与其他页面的联系 |
| --- | --- | --- |
| 学习空间 | 上传 PDF / Markdown / TXT，或描述目标并生成四节短学习手册 | 每个空间独立保存资料、对话、练习和笔记 |
| 学习 | 章节切换、记住阅读位置、PDF 原版与文字视图、原文划选、基于当前页或整份资料提问 | 讲解带来源按钮；可保存笔记、进入本节练习 |
| 图解 | 文本模型生成 Mermaid 关系图、静态 SVG 或分步过程图；关系图可缩放，步骤可前进、后退、播放 | 图解在原文区域展开；保留来源、可存入笔记 |
| 练习 | 按范围和知识点生成 3/5 道四选一理解题，提示、提交、判分、解析、错题回顾 | 题目依据要能对应原文；解析可回到原文或存笔记 |
| 笔记 | 新增、编辑、搜索、删除、Markdown 导出，显示关联图解 | 从讲解、图解或练习保存的笔记会保留来源位置 |
| API 设置 | OpenAI 格式 Chat Completions 服务、实际回复测试、保存、高级参数 | 一份文字模型配置供所有学习功能使用 |

答题记录会区分**独立答对**与**查看提示后答对**。同一道题重复点击或重发请求，不会增加答题次数，也不能覆盖第一次提交。需要再次验证时，可以重新生成一组题。

“学习记录”依据已提交题目显示未测试、待巩固、有通过记录、初步验证；不会把一次答对宣称为全面掌握。

## 数据在哪里

- `data/studytutor.db`：学习空间、提取的原文、对话、题目与提交记录、笔记、生成任务。
- `data/uploads/`：上传的 PDF 原文件。
- `data/pdf-pages/`：兼容阅读模式按需生成的原版页面缓存，不调用图像模型。大部头教材默认按页显示，也可在阅读器上方切换显示模式。
- `data/settings.json`：本机 API 配置，**含密钥，请勿分享**。整个 `data` 已加入 `.gitignore`。
- `data/server.log`、`data/server-error.log`：启动与访问日志，不记录请求体或密钥。
- `docs/screenshots/`：浏览器实拍的界面截图。

阅读位置和学习内容保存在本机，刷新或重启后保留。生成任务由后端执行，切换页面会继续；关闭后端会中断尚未完成的任务，重新打开后会标记中断，可以重试。

内部数据库文件名沿用初版的 `studytutor.db`，改名后已有资料和学习记录可继续使用。可用 `BOOKMIND_DATA_DIR` 指定数据目录，也兼容旧的 `STUDYTUTOR_DATA_DIR`。

## 启动、停止和修改

```powershell
# 本地启动；已启动时直接复用
powershell -ExecutionPolicy Bypass -File .\start.ps1

# 修改前端后重新构建
powershell -ExecutionPolicy Bypass -File .\start.ps1 -Rebuild

# 停止本项目后台进程
powershell -ExecutionPolicy Bypass -File .\stop.ps1
```

开发时开两个终端：

```powershell
# 后端：先停掉上面的后台服务，避免占用同一端口
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8018 --reload

# 前端，另一个终端
cd frontend
npm.cmd run dev
```

开发前端地址 http://127.0.0.1:5188；请求会代理到 8018。日常演示只需启动脚本，一个端口同时提供前后端。

迁移到其他电脑时：需要 Python 3.11+ 和 Node 22+，使用 `backend/requirements.lock.txt` 与 `frontend/package-lock.json` 安装。启动脚本会在缺少环境时尝试安装，缓存仍放在项目内。不要复制本机 `.venv` 或带密钥的 `data/settings.json` 给他人。

## 代码从哪里读

```text
BookMind-v2/
├─ backend/app/
│  ├─ main.py           # HTTP 接口、上传解析、后台任务、前端文件服务
│  ├─ learning.py       # 选取原文、模型任务、图解校验、出题、判分与学习记录
│  ├─ llm.py            # 本机模型配置、流式模型请求、错误处理
│  ├─ store.py          # SQLite 表和读写、任务状态、资料入库
│  └─ vendor/           # 复用的 DeepTutor 原文窗口与引文匹配辅助代码
├─ frontend/src/
│  ├─ App.tsx           # 首页、导航、学习空间共享状态、任务轮询
│  ├─ Learn.tsx         # 资料、阅读与助教
│  ├─ PdfReader.tsx     # PDF 画布与可划选文字层
│  ├─ Practice.tsx      # 练习生成、答题、解析、错题、学习记录
│  ├─ Notes.tsx         # 笔记编辑、来源跳转与导出
│  ├─ Settings.tsx      # API 配置与连接测试
│  ├─ components.tsx    # Markdown、数学公式、图解与分步播放
│  ├─ api.ts / types.ts # 请求封装与前后端数据类型
│  └─ styles.css / readability.css
├─ tests/               # 后端检查与 PDF 测试文件
├─ frontend/tests/      # Chrome 浏览器操作验收
├─ scripts/verify_live.py# 有费用的真实模型全流程验收（不会自动运行）
├─ docs/                # 架构说明、演示脚本、验证记录、截图
└─ start.ps1 / 启动BookMind.cmd / stop.ps1
```

实现主线见 [架构与改造说明](docs/架构与改造说明.md)，比赛演示可以参考 [演示脚本](docs/演示脚本.md)。

PDF 翻页与兼容阅读的更新说明见 [PDF 阅读修复记录](docs/PDF阅读修复记录.md)。

## 验证

```powershell
# 不调用模型，不消耗 API 费用
.\.venv\Scripts\python.exe -m pytest -q tests

cd frontend
npm.cmd run build

# 服务已启动且已准备示例学习空间后：浏览器操作测试
# 其中一项会向当前配置的模型发送一次“连接成功”测试请求
npm.cmd run test:ui
```

`scripts/verify_live.py` 会调用真实模型并新建演示数据，用于问答、图解、练习和手册的端到端验证；`scripts/verify_visual_formats.py` 另外验证 Mermaid 与 SVG。**不要作为每次启动的步骤**。首次开发验收已执行，结果保存在本机 `data/live-verification.json`。若在新电脑运行完整浏览器验收，需先用这两个脚本准备数据；日常使用不需要运行它们。

## 第一版的边界

- 暂无图像生成、语音、视频和扫描件 OCR。扫描 PDF 可以查看原版，但无法基于其中未提取到的文字问答或出题。
- “整份资料”采用本地关键词与中文二字片段检索：当前页加最多三个相关页，单次原文窗口最多约 16000 字符。没有宣称每次把整本书交给模型，也未继承 DeepTutor 的完整向量数据库与 Agent 工具系统。
- 生成的手册是短篇入门资料；图解与题目经过结构、来源校验，但教学内容仍可能出错。当前练习为四选一，没有主观题自动评分。
- 当前为单用户本地演示版，默认仅监听 `127.0.0.1`。没有多用户账号、班级管理或公开部署功能。

DeepTutor 代码来源与许可证见 [NOTICE](NOTICE)、[LICENSE](LICENSE)。
