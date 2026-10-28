# BookMind · 学迹

围绕同一本教材、同一组学习目标协作的 AI 学习助手。当前版本从 **DeepTutor 原版源码复制后删减、修改**，保留原版 Reading、Mastery Path、笔记、题卡和复习引擎；BookMind 增加教材关联层、统一导航和聚焦文字模型的 API 设置。

## 使用

双击 `启动BookMind.cmd`，打开 **http://127.0.0.1:8018**。前端使用原版 Next.js，后端在本机 `127.0.0.1:8019` 运行。首次启动需要 Python 3.11+、Node 22+ 和网络；依赖、缓存和数据放在项目目录。

1. 在「API 设置」填写兼容 Chat Completions 的地址、模型和密钥。阅读、图解、练习、笔记整理共用文字模型；测试连接会发送一次简短请求。
2. 导入 PDF、Markdown、TXT、EPUB 等资料。系统建立一个教材空间，关联原版阅读工作区、学习路径和笔记本。
3. 在顶部四个并列页面间切换：**自主学习、AI 导学、练习巩固、学习笔记**。自主学习按自己的节奏阅读和提问；AI 导学沿原版 Mastery 学习路径讲解、随堂测验并给出反馈。两种模式的会话分别保留，教材、知识点和学习记录互通。

初始学习目标来自教材目录，作为可编辑的学习骨架。大型 PDF 的完整书签留在阅读器，学习路径按章、节初始化；可通过原版路线编辑器和路径助教进一步细化，初始目录不代表模型已经逐页理解了整本书。

## 四个页面如何合作

| 页面 | 保留的原版能力 | BookMind 增加的联系 |
| --- | --- | --- |
| 自主学习 | 原版 Reading、对话、图解与阅读小测 | 关联本书目标与笔记，保留阅读位置和独立会话 |
| AI 导学 | 原版 Mastery 路径、教学会话与题卡 | 随堂测验、图解说明、补充示例；教材原文侧栏和经实际阅读验证的引用定位 |
| 练习巩固 | 原版完整 QuestionPipeline、QuizConfigPanel、QuizViewer、AI 评阅和题目追问 | 专项练习、错题本、复习计划；题数 1–50、难度、六种题型及配比；服务端校验教材范围并关联统一 assessment 日志 |
| 学习笔记 | 原版笔记本、记录编辑、搜索、总结、Markdown 导出 | 默认选择本书笔记本；保留目标与原文位置；路径助教可读取本书笔记 |

阅读、提问、收藏笔记不会直接提高掌握度。小测证据可进入同一知识点的复习安排，概念是否掌握仍遵守原版 Mastery 的验证规则。

专项练习每次选取同一知识点内最多 24 个原文单元，直接提供给原版出题流程，无需额外嵌入模型。客观题依据服务端保存的题目和答案判分；简答、论述、编程题支持原版 AI 评阅和追问，未判定的作答保留为待评估，不直接提高掌握度。本页聚焦教材出题，暂不提供原版参考试卷仿题入口。

## 原版源码在哪里

上游基线：[HKUDS/DeepTutor · a31d826](https://github.com/HKUDS/DeepTutor/commit/a31d82624c27d75f4e771d4fcb78dce16470b383)。许可证与第三方说明保留在 [LICENSE](LICENSE)、[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 和 [NOTICE](NOTICE)。

```text
deeptutor/
├─ reading/                    原版资料、页、标注、引用和小测引擎
├─ learning/                   原版学习目标、证据、掌握规则、复习调度与存储
├─ capabilities/reading/       原版阅读 Agent 与原文检索工具
├─ capabilities/mastery/       原版 Mastery Agent 与题卡工具
├─ services/notebook/          原版笔记服务
├─ services/session/           原版持久化会话与流式执行
├─ api/routers/                保留所需接口，删去其他功能的路由注册
└─ bookmind/                   新增：教材 ID 与以上模块的关联
web/
├─ components/reading/         原版阅读界面
├─ components/space/learning/  原版 Mastery Path 界面
├─ components/notebook/        原版笔记界面
├─ components/bookmind/        新增：书架、共同上下文、统一导航与设置
└─ app/                       Next.js 页面，已删减独立全局功能入口
```

原版公共运行时与共享组件保留；全局聊天首页、伙伴、看板、协同写作、课程管理、全局空间等页面已从当前应用入口移除。当前学习流程使用文字模型，不配置语音或图像生成服务。

架构及关联说明见 [原版模块融合说明](docs/原版模块融合说明.md)。

改版前的品牌、界面分工、原文引用及检索能力问题保存在 [问题快照](docs/当前版本问题清单.md)，用于对照。文字笔记与图解分开保存按原版设计保留。

以上问题清单保留了改版前快照。最新行为与验证见 [自主学习与 AI 导学改版说明](docs/AI导学改版说明.md)。当前仅启用文字模型相关工具及 SVG、图表、Mermaid、思维导图和 HTML 图解；联网、论文库、图像生成、视频及语音外部服务暂未启用，界面不会把它们当作已接通。

## 数据与迁移

- `data/user/workspace/reading/`：原版资料、原文、标注、位置，以及 BookMind 的教材关联数据库。
- `data/user/workspace/notebook/`：原版笔记。
- 学习路径与会话由原版 `LearningStore` 和 `SqliteSessionStore` 保存。
- `data/user/settings/`：本机配置，含 API 密钥；整个 `data/` 已忽略，不应提交。
- `.venv/`、`web/node_modules/`、`.cache/`：本机依赖和下载缓存，不提交。

本机新版目录是 `BookMind-v3`。`BookMind-v2` 保留原样；迁移脚本只读取旧版，复制资料与笔记。旧版练习不会凭名称猜测对应新目标、转成新的掌握证据。

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 -NoBrowser
powershell -ExecutionPolicy Bypass -File .\stop.ps1
# 修改前端后重新构建
powershell -ExecutionPolicy Bypass -File .\start.ps1 -Rebuild
```

## 验证

```powershell
.\.venv\Scripts\python.exe -m pip install pytest pytest-asyncio
.\.venv\Scripts\python.exe -m pytest -q tests/test_bookmind_native.py
.\.venv\Scripts\python.exe -m pytest -q deeptutor/learning/tests/test_mastery_state_integrity.py deeptutor/learning/tests/test_mastery_choices.py deeptutor/learning/tests/test_scheduler.py
cd web
npm.cmd run typecheck
npm.cmd run build
npm.cmd run test:unit -- tests/mastery-question-card.spec.tsx tests/mastery-question-group.spec.tsx tests/notebook-record-edit-discoverability.spec.tsx tests/reading-quiz-persistence.spec.tsx
```

`web/scripts/check-bookmind.mjs` 检查已经启动并已有示例资料的本机界面，截图存入忽略提交的 `data/native-verification/`。真实模型验证另行手动执行，普通构建和上述单元测试不调用模型。
