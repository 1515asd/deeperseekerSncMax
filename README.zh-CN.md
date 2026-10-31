<div align="center">

# DeeperSeeker

**自托管的 DeepSeek 网关，原生支持 OpenAI 与 Anthropic API 标准。**

[![License: MIT](https://img.shields.io/github/license/1515asd/rh-deeperseeker.svg)](LICENSE)
[![Last Commit](https://img.shields.io/github/last-commit/1515asd/rh-deeperseeker/main.svg)](https://github.com/1515asd/rh-deeperseeker/commits/main)
[![Stars](https://img.shields.io/github/stars/1515asd/rh-deeperseeker.svg)](https://github.com/1515asd/rh-deeperseeker/stargazers)
[![Forks](https://img.shields.io/github/forks/1515asd/rh-deeperseeker.svg)](https://github.com/1515asd/rh-deeperseeker/network/members)
![Release](https://img.shields.io/badge/release-v85_stable-2ea44f.svg)
![Branch](https://img.shields.io/badge/branch-english__available-blueviolet.svg)

把任何兼容 OpenAI / Anthropic 的客户端指向本服务即可使用——流式输出、工具调用、超长上下文会话管理开箱即用。

**适配客户端** · Claude Code · Cline · LibreChat · Open WebUI · LobeChat · Cherry Studio · Dify · 及一切 OpenAI/Anthropic 兼容客户端

[English](README.md) | [简体中文](README.zh-CN.md)

</div>

## 项目亮点

- **零成本 DeepSeek API** — 基于 DeepSeek 免费网页端运行，无需官方 API Key；同时提供标准 OpenAI Chat Completions 与 Anthropic Messages 接口
- **全网最快协议修复** — 2026-10-31 DeepSeek 更改网页协议、导致所有基于此反向 API 的代理集体失效，本仓库 **25 分钟内全球首个修复**，3 小时内发布加固稳定版
- **原生真实编号协议** — 完全对接 DeepSeek 服务器广播的真实消息编号，整条链路零猜测
- **工具调用泄露闸门** — 16 种已知形态的畸形工具调用标记在进入模型输出前被拦截
- **空流自动续写** — 上游 502 / 模型中途静默时自动续写（最多 2 次），不再出现"模型说着说着就停了"
- **超长上下文滚动接续** — 自动摘要交接，百轮以上长对话不再"聊着聊着就失联"
- **令牌池灾难恢复** — 多账号轮转、脏会话自愈、中毒会话自动重建
- **内置管理面板** — 用量、令牌池与请求监控开箱即用

## 为什么是这个 fork

上游 [`AmanCode22/deeperseeker`](https://github.com/AmanCode22/deeperseeker)（MIT）底子不错，但自 **2026-10-26 起停更**——10-31 协议变更后，跑老代码的所有部署全部失联。本 fork 持续维护免费层平台上的长期自托管部署：最近三周 110+ 次提交，紧急故障响应时间小于 1 小时。

**分支说明**：`main` 为开发线（高频热修）；**[`english`](https://github.com/1515asd/rh-deeperseeker/tree/english)** 为**全英文稳定线**——代码完全相同，注释、日志、会话内提示与面板界面全部为英文。想要纯英文体验请选它。

## v85 — 修复后的稳定版（请安装这个版本）

> **2026-10-31 DeepSeek 更改网页协议**：新对话的 `parent_message_id: 0` 写法被服务器拒绝
> （`biz_code 26 "invalid message id"`），且错误以纯 JSON 返回，旧解析器误判为空流。
> 所有基于此反向 API 的代理均受影响。

过渡期的协议修复提交（`v82`–`v84`）属于**紧急测试版**——如果用过它们遇到问题，那是预期内的。**本 v85 是修复后的稳定版，建议所有人安装这个版本。**

**如果 10-31 之前开始的旧对话出现失忆**，两种恢复方式：

1. **等待**：旧会话过期后（通常若干小时）重新发送消息，客户端会自动全量重发历史、记忆自动重建
2. **等不及就去 DeepSeek 官网**：登录网页版，在你的账号里找到那个会话，直接**点击删除**，然后即可新建会话

曾用过测试版的用户：请重新发送消息测试，一切应已恢复正常。

## 快速开始

**第一次用？先读[使用指南（中文）](docs/GUIDE.zh-CN.md)**——大白话讲清楚安装、面板、原理与常见问题。英文版见 [docs/GUIDE.md](docs/GUIDE.md)。

```bash
git clone https://github.com/1515asd/rh-deeperseeker.git
cd rh-deeperseeker
pip install -r requirements.txt
python app.py
```

Docker / Podman 部署、`.env` 配置、令牌设置与 API 端点说明，请查看 [英文 README](README.md) 的 **Quickstart**、**Configuration**、**Auth Token Setup** 与 **API Endpoints & Usage** 章节（内容一致）。

## 免责声明

⚠️ 自动化使用违反 DeepSeek 的服务条款。请使用专用小号，切勿使用个人主号；账号可能随时被封禁，风险自负。请勿滥用服务，仅限个人用途。
