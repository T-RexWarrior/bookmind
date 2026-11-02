# BookMind 面试学习包

> 目标：让你在 10 月 30 日早上 10:00 打开电脑后，不用再折腾环境，直接开始学。
>
> 这里有三份阅读材料、一个答题闯关应用，以及已经装好并跑通的项目。

## 在新电脑上使用

仓库：`https://github.com/T-RexWarrior/bookmind.git`，本工具位于 `interview-quiz/` 子目录。

```bash
git clone https://github.com/T-RexWarrior/bookmind.git
cd bookmind/interview-quiz
```

- 双击 `打开答题闯关.cmd`：需要本机有 Python 3，会启动 `127.0.0.1:8021` 的 SQLite 服务并打开页面。
- 没有 Python 时双击 `直接打开网页版.cmd`，答题记录只存浏览器 localStorage。
- 答题记录 `quiz_data.sqlite` 由本机自动生成，不随仓库同步；换机后从零开始，或按「学习记录与备份」一节导入 JSON。

---

## 一、现在是什么状态

### 已经完成

- 后端依赖已安装到 `bookmind/.venv`（Python 3.13.5）
- 前端依赖已安装到 `bookmind/web/node_modules`
- 前端已构建成功（Next.js 16）
- 后端已启动并验证：`http://127.0.0.1:8019/health/ready` 返回 `{"status":"ready"}`
- 前端已启动并验证：`http://127.0.0.1:8018/` 返回 200，页面可正常渲染
- 项目架构、术语、面试问答已整理成中文文档
- 答题闯关应用已用浏览器实际点通：58 道题（一刷 40 + 二刷 12 + 三刷 6）、28 个术语、三刷训练、12 分钟模拟面试、0/1/2 自评与间隔复习、闪卡、计划、Boss 计时
- 答题记录已接入本地 SQLite：`quiz_data.sqlite`，服务地址 `http://127.0.0.1:8021`，每次答题自动保存，并保留最近 200 个快照

### 需要你早上做的一件小事

项目能启动，但**还没有配置文字模型**。后端启动日志会提示：

```text
No active LLM model is configured. Please set it in Settings > Catalog.
```

项目根目录的 `.env` 里有 `BOOKMIND_LLM_BASE_URL`、`BOOKMIND_CHAT_MODEL` 和 `USTC_LLM_API_KEY`，但当前版本不会自动把 `.env` 写进本机设置。你需要在页面里填一次：

1. 打开 `http://127.0.0.1:8018`
2. 进入「设置」→「连接提供方」（也可以直接打开 `http://127.0.0.1:8018/settings/connections`）
3. 添加一个 OpenAI 兼容提供方，填：
   - API 地址：`https://api.deepseek.com/v1`
   - 密钥：复制 `bookmind/.env` 里的 `USTC_LLM_API_KEY`
4. 点「测试连接」
5. 进入「设置」→「选择对话模型」（`/settings/llm`），添加模型名 `deepseek-chat`，设为默认
6. 在页面底部应用更改，再到「设置」→「服务状态」确认可用

如果这个密钥失效，换成你自己的 DeepSeek / OpenAI 兼容密钥即可。**不要把密钥发到聊天或截图里。**

> 只做答题闯关不需要模型，所有学习内容都在本地 HTML 里。

---

## 二、10:00 怎么开始（ADHD 友好版）

### 主路线：白天打底，晚上三刷

| 时间 | 做什么 | 时长 |
| --- | --- | --- |
| 09:50 | 打开项目，确认 8018 能打开；打开闯关应用，做 Level 0 | 10 分钟 |
| 10:00 | Level 0 术语觉醒 | 15 分钟 |
| 10:15 | Level 1 检索两条路 | 20 分钟 |
| 10:35 | 休息：离开屏幕，喝水，走动 | 10 分钟 |
| 10:45 | Level 2 Agent 循环 | 20 分钟 |
| 11:05 | Level 3 项目全景 | 20 分钟 |
| 11:25 | 休息：远眺，活动肩颈 | 10 分钟 |
| 11:35 | Level 4 一次提问的旅程 | 20 分钟 |
| 11:55 | Level 5 证据与防幻觉 | 20 分钟 |
| 12:15 | 只做错题复习 | 15 分钟 |
| 下午 | Level 6 + 二刷场景诊断与架构取舍 | 35 分钟 |
| 晚上 | 三刷口述题 + 12 分钟模拟面试 + Boss 录音 | 40 分钟 |

### 如果今天只有 30 分钟

1. 做 Level 0（术语觉醒）
2. 做二刷的「场景诊断」前 3 题
3. 做三刷第 1 题：90 秒项目介绍，按评分标准自评
4. 看 `03-面试问答与讲稿.md` 的 Q6、Q8、Q19、Q20

### 注意力障碍友好的设计

- 每关只有 5 到 6 题，6 到 9 分钟，不要求一次坐两小时
- 一次只显示一道题，减少选择负担
- 答错不扣分，只把题放进错题队列
- 每题都有“为什么 + 项目里对应什么 + 面试一句话”
- 有 10 分钟专注计时器和 90 秒 Boss 计时器
- 进度存在浏览器本地，关掉再打开不会丢
- 计划表可以打勾，给大脑一个“完成”的信号

---

## 三、三刷训练法（这次升级的重点）

只做单选容易产生“我好像会了”的错觉。现在分成三层：

| 阶段 | 题量 | 题型 | 目标 |
| --- | --- | --- | --- |
| 一刷 · 基础 | 40 题 | 单选 | 建立词汇和直觉 |
| 二刷 · 应用 | 12 题 | 多选、排序、找错、场景诊断 | 能判断下一步怎么做 |
| 三刷 · 口述 | 6 题 | 白板口述 + 评分标准 | 能讲出来、能抗追问 |
| 模拟面试 | 8 题 | 4 基础 + 4 进阶，12 分钟 | 限时、无即时反馈、交卷复盘 |

配套机制：

- **0/1/2 掌握标记**：每道题答完后选“秒懂 / 想了一会 / 没思路”。秒懂会把复习间隔拉长，没思路会回到今天。
- **间隔复习**：按 1 / 3 / 7 / 14 天的节奏安排；错题立即进入复习队列。
- **弱项雷达**：按标签统计“曾经答对过”的比例，直接练最弱项。
- **错题复发**：错题不会消失，只有再次答对并自评后才移出。
- **答题模板**：概念题、选型题、项目题、设计题各有固定结构，避免临场乱答。

### 设计借鉴

这套训练法参考了这些公开经验，并且做了适合本项目的改造：

- [代码随想录](https://www.programmercarl.com/)：专题顺序、一刷/二刷/三刷、简单题感受方法、中等题解决问题、错题单独列、0/1/2 标记。
- [roadmap.sh](https://roadmap.sh/)：用可视化路线图回答“下一步学什么”。
- [system-design-primer](https://github.com/donnemartin/system-design-primer)：常见面试题 + 讨论 + Anki 间隔重复卡片。
- [coding-interview-university](https://github.com/jwasham/coding-interview-university)：用 checklist 管理学习进度。
- [awesome-fsrs](https://github.com/open-spaced-repetition/awesome-fsrs)：间隔重复的调度思想。

核心原则不是“刷 300 道”，而是“把 58 道讲透”：每道题都能说出 **定义、场景、项目例子、代价、追问**。

另外，选项顺序每次打开会重新打乱，正确答案不固定在 A；基础题的干扰项已改成“常见误解”，不要再靠“三短一长选最长”。

---

## 四、文件导航

| 文件 | 用途 | 什么时候看 |
| --- | --- | --- |
| `打开答题闯关.cmd` | 启动 SQLite 服务并打开学习应用（推荐） | 早上第一件事 |
| `直接打开网页版.cmd` | 不主动启动服务；8021 未运行时用浏览器本地 | 临时打开/服务起不来时 |
| `停止答题服务.cmd` | 停止 8021 端口的本地服务 | 学完或需要重启 |
| `答题闯关.html` | 58 道题 + 三刷训练 + 模拟面试 + 间隔复习 + 28 个术语 | 主入口 |
| `quiz_server.py` | 本地 SQLite 服务，无第三方依赖 | 需要长期保存时 |
| `quiz_data.sqlite` | 学习记录数据库（自动生成） | 备份时复制这个文件 |
| `01-零基础术语手册.md` | RAG、BM25、Embedding、Agent 等 30 个词 | 做题卡住时查 |
| `02-项目架构解析.md` | 技术栈、四页面、一次提问链路、设计取舍 | 第二遍学习时读 |
| `03-面试问答与讲稿.md` | 90 秒讲稿、30 个问答、STAR 故事 | 面试前一天背 |

### 答题闯关怎么用（推荐：SQLite 长期保存）

双击 `打开答题闯关.cmd`。它会：

1. 在 `127.0.0.1:8021` 启动本地 SQLite 服务；
2. 自动打开 `http://127.0.0.1:8021/答题闯关.html`；
3. 页面右上角显示「SQLite 已同步」。

学完要停服务，双击 `停止答题服务.cmd`。

如果不想启动服务，可以双击 `直接打开网页版.cmd`：8021 没有运行时，记录只存在浏览器 localStorage，右上角显示「浏览器本地」；如果 8021 已经在运行，页面仍会自动连接 SQLite。

手动启动服务：

```powershell
cd interview-quiz
python quiz_server.py --open
```

键盘操作：

- 答题：按 `1` 到 `4` 选择，`Enter` 提交 / 下一题
- 闪卡：空格翻面，左右方向键切换
- `Esc`：退出当前关卡

### 学习记录与备份

- **题目**：仍然硬编码在 `答题闯关.html` 的 `LEVELS`、`ADVANCED_LEVELS`、`GLOSSARY` 等常量里，不存数据库。
- **记录**：每次答题、自评、打勾、模拟面试交卷后，自动写入 `quiz_data.sqlite` 的 `quiz_state` 表；同时在 `quiz_snapshots` 表保留最近 200 个快照。
- **缓存**：localStorage 仍然保留一份副本；服务暂时不可用时自动降级为「浏览器本地」，服务恢复后点右上角状态徽章即可重新连接。
- **备份**：打开「计划」页 →「学习记录」，可以导出 JSON 备份、导入备份；也可以直接复制 `quiz_data.sqlite`（建议先停止服务）。
- **跨设备**：复制 `quiz_data.sqlite` 到另一台机器，或导出 JSON 再导入。
- **不会丢**：清浏览器缓存不会删除 SQLite 记录；删除 SQLite 文件才会清空。当前记录在 `quiz_data.sqlite`，与 BookMind 项目的 `bookmind/data/` 是两套独立数据。
- **接口**：`GET /api/health`、`GET /api/state`、`POST /api/state`、`GET /api/export`。服务只绑定 `127.0.0.1`，不对局域网开放。

---

## 五、项目怎么启动 / 停止

### 当前已经在运行

- 前端：`http://127.0.0.1:8018`
- 后端：`http://127.0.0.1:8019`
- 健康检查：`http://127.0.0.1:8019/health/ready`
- 答题记录服务：`http://127.0.0.1:8021`（`/api/health` 返回 `storage=sqlite`）

如果电脑重启或进程被关掉，用下面任一方式重启。

### 方式 A：一键脚本（推荐）

```powershell
cd "E:\SA25007138+134334379885+智能体赛道+研究生队伍\bookmind"
powershell -ExecutionPolicy Bypass -File .\start.ps1 -NoBrowser
```

它会复用已经装好的 `.venv` 和 `web/node_modules`，不会重新下载依赖。前端已构建过，也不会重新构建。

停止：

```powershell
powershell -ExecutionPolicy Bypass -File .\stop.ps1
```

### 方式 B：手动启动

```powershell
cd "E:\SA25007138+134334379885+智能体赛道+研究生队伍\bookmind"
.\.venv\Scripts\python.exe -m uvicorn bookmind.main:app --host 127.0.0.1 --port 8019
```

另开一个终端：

```powershell
cd "E:\SA25007138+134334379885+智能体赛道+研究生队伍\bookmind\web"
npm run start -- --hostname 127.0.0.1 --port 8018
```

### 如果启动失败

看日志：

- 后端：`bookmind/data/backend.log`
- 前端：`bookmind/data/frontend.log`

常见问题：

- 端口被占用：先运行 `stop.ps1`，或检查 8018 / 8019
- 模型没配置：页面能打开，但对话会失败；按第一节配置模型
- 依赖损坏：重新执行 `python -m pip install -e .`（在项目根目录，使用 `.venv` 里的 Python）

---

## 六、这个项目到底是什么（先看这一段）

BookMind 是一个**教材驱动的 AI 学习助手**：

```text
导入教材 → 阅读原文 → 规划学习目标 → 做练习 → 记笔记 → 复习
```

技术上：

- 前端：Next.js 16 + React 19
- 后端：FastAPI + WebSocket
- 核心：单 Agent 循环 + capability 工具插件
- 检索：阅读用 locator 全文检索；知识库用向量 + BM25 + 混合检索
- 存储：SQLite + 文件

最重要的一句话：

> 阅读场景不用向量库，因为学习更关心“这句话在哪一页”，而不是“语义相似”。

---

## 七、学习时不要做的事

- 不要一开始就读完整套代码。先按关卡建立地图。
- 不要背术语定义。每个词都要能说出“它解决什么问题、有什么代价”。
- 不要把“用了 RAG”当成答案。要能解释为什么这里用 locator，而不是向量。
- 不要虚构自己做过所有模块。面试时按真实参与说，不熟的说“读过代码/了解设计”。
- 不要连续学两小时。20 分钟起身一次，注意力会更好。

---

## 八、内容依据

- 项目版本：`bookmind` 当前 `main` 分支 `ff03c4c`
- 主要依据：`README.md`、`docs/architecture.md`、`docs/development.md` 以及实际代码
- 代码里的关键位置：
  - 阅读检索：`bookmind/reading/search.py`
  - 阅读能力：`bookmind/capabilities/reading/capability.py`
  - 阅读工具：`bookmind/capabilities/reading/tools.py`
  - Agent 循环：`bookmind/agents/loop/agent_loop.py`
  - 工具调度：`bookmind/runtime/agentic/tool_dispatch.py`
  - 混合检索：`bookmind/services/rag/pipelines/llamaindex/retrievers.py`
  - 学习证据：`bookmind/learning/lesson_evidence.py`
  - 复习调度：`bookmind/learning/scheduler.py`
- 答题记录：本目录 `quiz_server.py` + `quiz_data.sqlite`，与 BookMind 后端的 SQLite 无关

如果代码后续更新，个别细节可能变化；面试前可以对照当前分支再确认一遍。

---

## 九、早上 10:00 的第一句话

双击 `打开答题闯关.cmd`，等右上角显示「SQLite 已同步」，点「继续：术语觉醒」。

不需要先准备好，先做第一题。
