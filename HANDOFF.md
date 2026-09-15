# BookMind 交接记录（2026-09-15）

> 面向没有任何前文的新接手者。本文记录本次在**源码**上完成的调试与修复、设计取舍、验证方式和剩余风险。它不是发布说明；当前工作区仍处于未打包的开发调试状态。

## 1. 项目与分工边界

BookMind 是一个面向教材学习的本地 Web 应用：PDF 摄取和解析 -> 知识点图谱/检索 -> 教材问答 -> 自适应出题 -> 判分与证据写入 -> 学习状态（L1--L4、复习、误区）。全链路图见 `BookMind工作流程.mmd`。

原定协作分工为：

| 范围 | 负责人 | 本次是否涉及 | 本次原则 |
| --- | --- | --- | --- |
| 教材处理：上传、PDF 解析、切块、索引、知识图谱 | 队友 | 仅排障关联 | 没有重写摄取链路；只在问答降级时过滤不适合展示的解析残片。 |
| 教材问答：检索、上下文、带引用回答 | 队友 | 有小范围修正 | 保留“只基于资料、不伪造答案”的边界；修复模型不可用时直接暴露乱码/代码片段的问题。 |
| 测验与判分：出题、提示、跳过、提交、诊断、讲解、下一题 | 本人 | 是，主要工作 | 使题目、标准答案、Rubric、判分与生命周期成为闭环。 |
| 学习状态：证据、掌握等级、薄弱项、推荐队列、学习档案 | 本人 | 是，主要工作 | 让每次独立作答可见、可刷新，并让推荐题遵循当前知识点和等级。 |

结论：本次工作总体遵守原分工。对教材问答的改动是为避免其离线降级内容污染测验/对话体验，并未改变队友负责的检索、解析和图谱主流程。

## 2. 当前架构和关键入口

### 后端

- 应用入口：`backend/bookmind/api/app.py`
- 会话意图与状态机：`backend/bookmind/services/conversation_orchestrator.py`
- 任务全生命周期：`backend/bookmind/services/task_service.py`
- 出题器：`backend/bookmind/engine/task/generator.py`
- 判分 Agent：`backend/bookmind/agents/diagnostician.py`
- 教材问答/离线降级：`backend/bookmind/agents/tutor.py`、`backend/bookmind/services/book_qa.py`
- 学习状态投影：`backend/bookmind/services/learner_state_view.py`
- 学习档案接口：`backend/bookmind/api/routes/projects.py`
- 任务接口（生成、提交、提示、跳过、讲解）：`backend/bookmind/api/routes/tasks.py`

### 前端

- 会话动作：`frontend/src/features/conversations/useConversationActions.ts`
- 会话区与题目按钮：`frontend/src/features/conversations/ConversationPane.tsx`
- 卡片组件：`frontend/src/components/cards/Cards.tsx`
- 消息渲染与数学/Markdown：`frontend/src/components/MessageTimeline.tsx`、`frontend/src/components/MathText.tsx`
- 学习档案抽屉：`frontend/src/features/learning/LearningSidebar.tsx`
- 练习/评估概览和模式：`frontend/src/features/assessment/ConsolidationOverview.tsx`、`ModeSwitcher.tsx`

### 数据与不变量

- `TrustedTaskContext` 保存题干、标准答案、Rubric、目标知识点与任务状态，是判分/讲解的唯一可信题目上下文。
- `LearningEngine` 是学习证据和掌握状态的唯一写入口；问教材得到的 `QUESTION` 信号**不能**直接升级掌握等级。
- 独立完成的 `PASS` 才能构成升级证据；提示、跳过、先看讲解不应伪造“已掌握”。
- 任务状态至少应有唯一出口：`PENDING -> 已提交判定 / SKIPPED / EXPLAINED`。任何按钮不能只改变前端焦点而不改变或解释状态。

## 3. 本次修改：问题、根因、修复

### 3.1 LLM 配置、出题契约与判分

**现象**

- 一度默认离线，或模型网关超时后不断显示“暂无法可靠判断”。
- 在线模型生成题干后却使用本地泛化的标准答案/Rubric，题目和判分依据可能不匹配。
- 诸如 `O(n)`、数字结果等可核对的简短回答，被前置长度规则当作“信息不足”。

**修复**

1. `backend/bookmind/config.py`：`.env` 改为按项目根目录加载，不再因从 `backend/` 目录启动而读不到根目录配置；仍允许环境变量覆盖。
2. `backend/bookmind/api/dependencies.py`：主聊天调用超时设为 **25 秒**；DeepSeek 地址只使用当前模型，不再在失败后请求不属于 DeepSeek 的 GLM 模型。真实密钥仍只存在 `.env`，绝不提交。
3. `backend/bookmind/engine/task/generator.py`：在线出题改为严格返回 `prompt_text + expected_answer + rubric` 的 JSON，并校验它们是同一题的闭合契约；无效/超时时降级为本地模板题，并在题卡上明确显示“在线生成”或“离线兜底”。内置演示题仍是无模型、可稳定运行的 curated 题。
4. `backend/bookmind/agents/diagnostician.py`：判分 prompt 显式带入真实题干、服务器端标准答案和 Rubric；要求模型将明确错误/部分正确判为 `FAIL/PARTIAL`，不能用 `NEEDS_REVIEW` 回避。对于超时，只允许用标准答案与作答共同出现的公式/数字生成保守 `PARTIAL`，绝不离线误判为 `PASS`。
5. `task_service.py`：删除“答案太短就不送判分”的错误门槛；`O(1)`、选项、符号等仍会进入判分。

### 3.2 题目完整生命周期与自然语言动作

**现象**

- “补充答案”只聚焦输入框，看起来按钮失效。
- “我不知道”被当成教材追问，出现“资料中没有与‘我不知道’相关信息”。
- “提示/跳过/讲解/下一题”存在互相抢状态或没有唯一出口的情况。

**修复**

1. `conversation_orchestrator.py` 新增 `REQUEST_HINT`、`SKIP_TASK`、`UNSURE_OR_GIVE_UP` 意图。
2. 在线时由小型 JSON 意图分类调用理解自然语言；置信度不足、断网或离线时回落到确定性关键词规则。LLM **只理解意图，绝不直接写学习状态**。
3. “我不知道/不会”展示显式选项卡：提示、查看讲解并结束本题、提交答案、跳过本题。评估模式隐藏提示。
4. `Cards.tsx` / `ConversationPane.tsx`：补充答案在输入框已有文字时直接提交，否则明确聚焦输入；判定为 `NEEDS_REVIEW` 时同时提供讲解结束和跳过出口。
5. `tasks.py` / `task_service.py`：允许 `PENDING` 题查看讲解，但会转为 `EXPLAINED`，并提示“本次不写作答证据”；跳过转为 `SKIPPED`，不记错。这样不会卡住旧题，也不会把看答案当作独立通过。

### 3.3 讲解质量、资料摘录与公式渲染

**现象**

- “看讲解”调用宽泛的教材问答，得到目录/原文摘录；资料不足就拒绝讲解。
- 模型不可用时展示 PDF 解析乱码以及 `class Fib`、`int prev()` 等代码残片。
- 题目与讲解里的数学公式和 Markdown 没有渲染，长讲解显得被截断。

**修复**

1. 讲解接口不再走泛化问答检索：优先使用当前 `TrustedTaskContext` 的题干、标准答案和 Rubric 生成“怎么想/参考结论/检查点/常见误区”；模型不可用时用同一可信上下文生成结构化本地讲解。资料定位只用于回原文，不再冒充讲解本身。
2. 讲解模型输出限制为不超过约 650 个中文字并要求完整收束，避免不完整长回复。
3. `TutorAgent._safe_excerpt` 只在离线时展示可读、足够长且不含乱码/代码结构的摘要；否则只报告已定位页码并建议回原页/稍后重试。
4. 新增 `frontend/src/components/MathText.tsx`，引入 KaTeX，安全地渲染 `$...$`、`$$...$$`、`\\(...\\)`、`\\[...\\]` 和常用 Markdown（标题、列表、引用、代码、加粗）。不渲染模型返回的原始 HTML，避免 XSS；相应样式在 `app.css`。
5. `frontend/package.json` / lockfile 添加 `katex`；`frontend/dist` 随构建更新并包含 KaTeX 字体，保留离线演示包可用性。

### 3.4 学习状态、薄弱项与层级

**现象**

- 连续答题后右上“学习状态”显示“尚无学习记录”，甚至提示“服务内部有错误”。
- 左侧薄弱项完成后不即时消失，刷新浏览器后才变化。
- 总显示“缺少一次独立作答证据”，即使已从 L1 做到更高层级。
- 有实际作答的概念在 L0 时看起来像完全未学习。

**修复**

1. `projects.py` 的学习档案接口加入作答次数与最近作答结果；修复直接导致 500 的错误：`LearnerStateView` 内的 `EvidenceView.result` 已经是字符串，不能再访问 `.value`。
2. `LearningSidebar.tsx` 每次打开抽屉都会重新请求学习摘要和误区数据；原来缓存的 `null` 不会再被误显示为“无学习记录”。作答后侧栏刷新成功时也会立刻更新。
3. `learner_state_view.py`：对独立的 L0 `PARTIAL/FAIL` 归为 `weak`，不再归到“未触及”。学习档案显示“作答 N 次”，而不是只显示“问过 N 次”。
4. `TaskService._next_quiz_level` 修复初始等级：未验证概念的第一题必须是 L1，之后按 L1 -> L2 -> L3 -> L4 递进。
5. 题卡“为什么现在问”按真实目标层级写清 L1 基础验证、L2 解释应用、L3 迁移、L4 综合，而不是永远一条 L1 文案。

### 3.5 推荐队列、下一题与评估模式

**现象**

- 点 1.2 出题后，“下一题”或“开始推荐练习”跳回 1.1。
- “开始推荐练习”点击后有时看似没有反应，或与前端会话刷新竞争。
- UI 中没有独立、清楚的“能力评估”工作流。

**修复**

1. 前端下一题现在传递 `from_task_id`。后端读取该题目标知识点：未到 L4 时留在同一知识点继续上一级；达到 L4 后按同一资料的 mapper 顺序选下一个未掌握兄弟知识点，且**不回绕**到 1.1。
2. `TaskService.request_task` 的“默认推荐”也统一走 `consolidation_candidates`。以前只有显式筛选才走队列，默认请求会落到图谱首节点，正是“总回 1.1”的根因。
3. 推荐排序优先已经通过 L1/L2/L3、应继续验证的概念，再处理未做的 L0；保留 mapper 原始顺序，不按名称重排。
4. 创建任务接口直接返回已持久化的消息卡，前端立即 append；只有幂等重试命中旧 PENDING 题时才重新拉取会话。这消除了“实际创建成功但页面看起来没动”的竞态。
5. 前端恢复独立 `ASSESSMENT` 模式，练习/评估使用不同会话与文案；评估隐藏提示，避免把带提示的作答当成独立证据。

### 3.6 不强行把教材问题归类为错误知识点

**现象**

“时间复杂度是什么”被错误归到“串”。这会污染薄弱项与推荐队列。

**修复**

`conversation_orchestrator.py` 调整问题到知识点的匹配：只有名称、别名或较强来源上下文支持时才写 `QUESTION` 信号；无法可靠归类则显示“已保留提问，但不会写入学习状态”。宁可漏记一次，也不把错误概念写成学习证据。

## 4. 本次验证记录

在项目根目录执行过：

```powershell
$env:BOOKMIND_LLM_API_KEY_ENV='BOOKMIND_TEST_NO_KEY'
D:\anaconda\envs\bookmind\python.exe -m pytest tests\test_task_generator.py tests\test_learner_state_view.py tests\test_action_interpreter.py -q --basetemp .pytest-tmp-final
```

结果：**22 passed**。另已运行：

```powershell
# 后端语法
cd backend
D:\anaconda\envs\bookmind\python.exe -m py_compile bookmind\services\task_service.py bookmind\api\routes\tasks.py bookmind\api\routes\projects.py

# 前端
cd ..\frontend
npm run typecheck
```

均通过。后端重启后 `GET http://127.0.0.1:18765/health` 返回 `{"status":"ok","version":"0.1.0"}`；开发前端 `http://127.0.0.1:5173/ui/` 返回 200。

建议在继续开发前补充端到端测试，至少覆盖：连续 L1--L4 均通过、L0 失败后薄弱项即时更新、PENDING 题的四种出口、DeepSeek 超时、在线题 JSON 契约不合格的离线回退、以及 `from_task_id` 到下一概念的导航。

## 5. 本机开发运行方式

当前使用 `conda` 环境 `bookmind`，并行启动后端和 Vite 前端：

```powershell
# 终端 1：从 backend 启动，根目录 .env 会被 config.py 自动读取
Set-Location .\backend
D:\anaconda\envs\bookmind\python.exe -m uvicorn bookmind.api.app:app --host 127.0.0.1 --port 18765

# 终端 2：前端热更新
Set-Location .\frontend
npm run dev -- --host 127.0.0.1 --port 5173
```

开发地址是 `http://127.0.0.1:5173/ui/`；打包版由后端在 `http://127.0.0.1:18765/ui/` 提供。前端改动通常 HMR 即时生效，后端 Python 改动应重启 Uvicorn。若浏览器仍是旧静态资源，使用 `Ctrl+F5`。

**重要：**默认 SQLite URL 是相对路径。`start.ps1` 从项目根运行，而直接从 `backend/` 启动会使用不同的相对 `data` 位置。调试时务必固定启动目录，或显式设置 `BOOKMIND_DATABASE_URL` 和 `BOOKMIND_DATA_DIR`，否则会误以为学习记录“丢失”。

## 6. 已踩坑、经验和教训

1. **题干与判分依据必须同源。** “LLM 只生成题干 + 本地通用答案”表面稳定，实际会制造无法判对的题。要么一并生成并验证闭合契约，要么完全使用本地题库。
2. **`NEEDS_REVIEW` 不是错误答案的垃圾桶。** 它只适用于不可辨认/无依据；明确错误应记 `FAIL`，部分正确应记 `PARTIAL`，否则学习状态永远不动。
3. **所有任务操作都要有状态出口。** 仅用文本关键词不能覆盖自然表达；LLM 可以做意图理解，但状态转换必须仍由服务端状态机决定，且离线规则必须可用。
4. **学习状态是服务端投影，前端不能把初始空值当事实。** 抽屉打开和作答后都要刷新；接口出现 500 时必须显示真实错误，不能静默渲染“无记录”。
5. **不要从“刷新后好了”推断前端没问题。** 此次一个 `.value` 访问字符串的后端异常，掩盖成了前端缓存问题；应先看网络响应/服务日志。
6. **推荐算法必须保留上下文。** “下一题”和“开始推荐练习”不能一条走当前知识点、一条走图谱首节点。入口应汇入一个队列/选择策略，任务应携带来源 task id。
7. **RAG 原文不是任何时候都适合直接展示。** PDF/OCR/代码教材可能含残片。离线时宁可诚实说模型不可用并定位原页，也不要把乱码当解释。
8. **Markdown/公式渲染要白名单化。** 采用受控解析 + KaTeX，不把模型文字作为 HTML 注入。
9. **配置加载与启动目录是 Windows 本地演示的高风险点。** `.env` 固定按项目根读取已修复；SQLite 相对路径仍需要开发者主动统一。
10. **Windows 测试清理可能锁 SQLite。** 大范围 API 测试会因 TestClient/SQLite 文件句柄产生清理警告；本次用独立 `--basetemp` 运行聚焦测试。不要因为缓存目录权限警告误判业务测试失败。

## 7. 剩余风险与下一步

- 没有完成真实 DeepSeek 的长时间端到端压测；25 秒是交互上限，不保证网关稳定。应记录模型延迟、超时率、JSON 合约失败率和降级次数。
- 在线题仍应人工抽样检查“资料支持性”；虽然 Task Validator 约束了格式，但无法完全证明模型没有引入教材外知识。
- 当前 L1--L4 的升级依赖既有 Evidence Gate 规则。请在真实数据库里逐题检查每一层 `PASS/PARTIAL/FAIL` 是否符合竞赛展示语义，而不是只看 UI 文案。
- `运行说明.txt` 与 `源码审阅说明.md` 仍描述旧的 USTC/Qwen/GLM 默认配置；本地 `.env` 已可改为 DeepSeek，但提交前/演示前应统一文档与实际环境，且不得提交密钥。
- 这次只做了源码调试，**没有重新打包 `.exe`**。最终交付前需要在干净环境构建 `frontend/dist`、打包、使用全新数据目录做一次从上传 PDF 到 L4 的验收。
- `frontend/dist` 是离线演示包的一部分，已随 KaTeX 更新；不要在后续提交中随意删除 hash 资源或字体。

## 8. 建议的人工验收脚本

1. 打开已有项目，点右上“学习状态”：应能看到作答次数和分组，不应出现“服务内部错误”。
2. 在 1.2 连续独立答对：题目层级应从 L1 逐步上升；学习档案和左侧薄弱项应在无需刷新浏览器的情况下变化。
3. 对 PENDING 题分别点“给提示”“提交答案”“查看讲解并结束本题”“跳过本题”：每个动作均有可见反馈，且不应遗留无法处理的待完成题。
4. 对 `NEEDS_REVIEW` 卡片：输入补充内容后点“提交补充答案”应真实提交；无输入时按钮应把焦点定位到输入框。
5. 从 1.2 的结果卡点“下一题”：未 L4 时应仍为 1.2 的下一等级；L4 后应移到后续知识点，不应回到 1.1。
6. 在无网络/无 API key 情况提问：应显示模型不可用和可读摘要/页码，不应显示乱码、OCR 残片或代码碎片。
7. 使用含 `$O(n)$` 或 `T(n)=2T(n/2)+n\\log n` 的题目与讲解：应渲染数学公式，Markdown 标题/列表不应以原始符号堆在页面上。

