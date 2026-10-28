<p align="center">
  <img src="web/public/favicon.svg" width="72" alt="BookMind 芽苗图标" />
</p>

<h1 align="center">BookMind · 学迹</h1>

<p align="center"><strong>从一份资料或一个目标出发，读懂、练会、记下来。</strong></p>

<p align="center">
  <a href="LICENSE"><img alt="Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-175b4b" /></a>
  <img alt="Next.js + FastAPI" src="https://img.shields.io/badge/Next.js%20%2B%20FastAPI-333333" />
</p>

BookMind 是面向教材自学与目标学习的 AI 学习助手。阅读、AI 导学、练习和笔记围绕同一份资料、同一组知识点协作，让一次问答变成可以持续推进的学习过程。

## 两种开始方式

- **已有资料**：导入教材或文档，边阅读边提问，结合原文理解概念。
- **已有目标**：说明想学什么，由 AI 组织内容、生成学习资料，再进入阅读、讨论与练习。

## 核心功能

| 模块 | 可以做什么 |
| --- | --- |
| 自主学习 | 阅读原文、讨论疑问、查看引用，用图解和示例辅助理解 |
| AI 导学 | 围绕知识点循序讲解，通过追问与随堂测验检查理解 |
| 练习巩固 | 自选知识点、题量、难度和题型，作答后查看解析、评阅与错题复习 |
| 学习笔记 | 保存摘录与总结，关联教材位置和知识点，回到原文继续学习 |

各模块共享资料与学习记录，保留各自独立的对话。导学和练习的有效评估汇入同一份掌握记录。

## 界面预览

**自主学习：原文与讨论并排，图解辅助理解。**

![BookMind 自主学习](docs/images/self-directed-learning.png)

**练习巩固：按知识点配置练习，作答后查看结果与解析。**

![BookMind 练习巩固](docs/images/practice.png)

## 快速开始

准备 Python 3.11–3.14 与 Node.js 22+，克隆项目后运行：

```powershell
git clone https://github.com/T-RexWarrior/bookmind.git
cd bookmind
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

打开 **http://127.0.0.1:8018**，在「API 设置」中填写支持工具调用的文字模型，再导入资料或创建学习目标。

首次启动会安装依赖并构建前端。[安装与开发](docs/development.md)包含手动启动、环境配置、数据目录与验证方式；[架构说明](docs/architecture.md)介绍模块职责和协作过程。

## 开源与致谢

BookMind 基于 [HKUDS/DeepTutor](https://github.com/HKUDS/DeepTutor) 开发，复用并改造其阅读、导学、资料生成、练习、笔记与会话引擎。采用 [Apache License 2.0](LICENSE)，原始版权与第三方声明见 [NOTICE](NOTICE) 和 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
