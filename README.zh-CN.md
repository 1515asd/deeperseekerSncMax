<div align="center">

# DeeperSeeker

**自托管的 DeepSeek 网关，原生支持 OpenAI 与 Anthropic API 标准。**

[![License: MIT](https://img.shields.io/github/license/1515asd/rh-deeperseeker.svg)](LICENSE)
[![Last Commit](https://img.shields.io/github/last-commit/1515asd/rh-deeperseeker/main.svg)](https://github.com/1515asd/rh-deeperseeker/commits/main)
[![Stars](https://img.shields.io/github/stars/1515asd/rh-deeperseeker.svg)](https://github.com/1515asd/rh-deeperseeker/stargazers)
![Release](https://img.shields.io/badge/release-v89-2ea44f.svg)

把任何兼容 OpenAI / Anthropic 的客户端指向本服务即可使用——流式输出、工具调用、超长上下文会话管理开箱即用。

**适配客户端** · Claude Code · Cline · LibreChat · Open WebUI · LobeChat · Cherry Studio · Dify · 及一切 OpenAI/Anthropic 兼容客户端

[English](README.md) | [简体中文](README.zh-CN.md)

</div>

> [!WARNING]
> **旧版用户请升级**：如果你在运行 v89.6 或更早的版本，请尽快更新到 **v89.7**。旧版有两个会影响你的问题——
> ①被禁言的账号会被显示成「正常」，用户请求时报 `502 "biz error 0"`；v89.7 起按上游硬证据识别禁言并立即隔离（附解禁时间）。
> ②v89.6 之前，面板的「恢复」按钮和文件上传接口每次调用必报 500。
> 升级方式：拉取最新代码并重启即可，数据与令牌池自动兼容，无需任何手动迁移。升级后可在面板令牌池点「检测令牌」，逐个查看每个令牌的真实状态（正常 / 禁言到几点 / 已作废）。

## 这是什么

本项目基于上游 [`AmanCode22/deeperseeker`](https://github.com/AmanCode22/deeperseeker)（MIT），把 DeepSeek 网页免费层包装成标准 API：无需官方 API Key，同时提供 OpenAI Chat Completions 与 Anthropic Messages 两套接口。本 fork 在其基础上维护免费层平台上的长期自托管部署，持续跟进协议变更与稳定性修复。

## 更新在哪看

- **开发线（main）**：[提交历史](https://github.com/1515asd/rh-deeperseeker/commits/main)
- **英文稳定线（english）**：[提交历史](https://github.com/1515asd/rh-deeperseeker/commits/english)

`main` 为开发线，修复合入后 `english` 线会在数日内跟进同步（同步时保证全部内容为英文）。每个版本做了什么，见下表：

| 版本 | 日期 | 内容 |
|---|---|---|
| v89.7 | 11-05 | 禁言检测修复（生产实锤 11-5 晚，禁言账号被显示成「正常」/误报 502）：①上游禁言报文藏在嵌套信封里（外层 code=0、内层 biz_code=5 "user is muted"），旧代码只读外层 → 502 "biz error 0" 且不进任何隔离通道——现在按上游 `is_muted==1` 布尔硬证据识别，命中即隔离（原因附解禁时间）；②新增「确认禁言」状态档（与「疑似禁言」区分）；③令牌池新增「检测令牌」按钮：逐个直接询问上游真实状态（正常/禁言到几点/已作废），确认禁言自动隔离；④启动时静默扫描 ACTIVE 令牌，禁言账号不再带着「正常」标签过夜。全部改动只影响失败路径，成功路径逐字节不变 |
| v89.6 | 11-05 | 三类修复（生产实锤 11-5 晚）：①恢复端点自 v77 起 NameError 必 500、文件上传端点同样必 500——两个 NameError 全部修复并全库静态扫描确认无同类残留；②面板状态体系重做——隔离原因落库，「疑似过期 / 疑似禁言 / 其他隔离（含具体原因）」五态五色显示，禁言类业务错误不再冷却后假装正常，每种状态配大白话说明与自查指引，恢复按钮永远安全（真失效会自动再隔离）；③修复 effort 指令链断裂——`reasoning_effort=max` 的强化指令首次真正生效，面板「功能开关」可随时关闭（关闭后与旧版行为逐字节一致） |
| v89.5 | 11-05 | 裸 JSON 工具调用由「退回重写」升级为「确定性自动修复」：四道闸门（整段恰好一个 JSON、工具名命中本轮工具表、arguments 为字典、schema required 全覆盖）全过时，抢救层直接补回丢失的 `<tool_call>` 包裹走既有解析投递链——零额外上游往返、参数原样保留、会话历史不再被泄露 JSON 污染；任一闸门不过则退回 v89.4 重写兜底，下限与上一版逐字节一致 |
| v89.4 | 11-05 | 裸 JSON 工具调用自愈：模型偶发把工具调用写成不带 `<tool_call>` 包裹的裸 JSON（生产实证 11-5 00:23），原样漏进正文且会话停摆。现命中「整段正文恰为一个 JSON、工具名命中本轮真实工具表」时走既有 v62 诊断续写——桥接层不执行任何东西，由模型自行重发正确调用或改给最终答案。正常回复逐字节不变 |
| v89.3 | 11-04 | 隔离零误判：HTTP 200 业务级错误的「鉴权措辞」判定属于软证据，单次命中不再施加任何惩罚（误判零代价）；连续 3 次（可配）才隔离，成功一轮自动清零。真 HTTP 401（硬证据）仍立即隔离。修掉 v88 唯一残余——英文措辞碰巧含 auth 类单词会把健康账号永久踢下线 |
| v89.2 | 11-04 | 重复调用闸门文案改为纯事实陈述：只说「此命令此前原样出现过 N-1 次、本条 Result 是本次真实执行所得」，零指令、零推断——与用户要求重复的指令不可能冲突，也不会在结果真丢失时替丢失的结果背书 |
| v89 | 11-04 | 重复调用闸门（基于 11-1 生产事故复盘）：同一命令被原样重发 ≥2 次时，自动向结果注入一条系统备注。仅在死循环正在发生时介入——正常请求的提示词与此前版本逐字节一致 |
| v88 | 11-04 | 移植上游 PR #94 的加固：HTTP 200 里携带的业务级错误（账号被标记、网页会话被杀、频控墙）现在会被正确分类并触发令牌轮转，不再把死号钉死；新会话创建 / PoW 阶段的失败纳入重试体系，且重试在签名锁外执行，不再把请求挂死 |
| v87 | 11-04 | 修复「对话失忆」链。根因是本 fork 此前的两项优化（PoW 凭证预取、空流自动续写）组合引入的——上游没有预取池和续写兜底，不存在此问题：预取的凭证不再过期 → 空闲后带陈旧凭证请求被拒（40301）→ 被误判为空流 → 自动续写当成新消息发出 → 会话映射中毒。现在 PoW 凭证强制 10 分钟有效期、业务错误正确分类、自动续写只在确认上游受理后触发。修复对客户端完全透明（HTTP 200） |
| v86 | 11-01 | 管理面板重新设计，集成深度健康检查；新增 Docker 部署文件（PR #1，感谢 @anch665） |
| v85 | 11-01 | 10-31 协议变更的稳定版：原生消息编号协议（首条消息省略 `parent_message_id`、后续轮次使用服务器广播的真实编号）、空流自动续写、工具标记泄露拦截、旧对话自愈。**建议所有人安装此版本或更高** |
| v82–v84 | 10-31 ~ 11-01 | 协议变更当日的紧急修复（测试版） |

> **跟进官方项目的问题反馈**：我们会持续关注上游官方项目（[issues](https://github.com/AmanCode22/deeperseeker/issues) / PR）中的问题报告与协议变动，凡与本仓库场景相关的修复都会尽快评估并移植进来——v88 即移植自上游 PR #94。

## 本 fork 加了什么

基础能力——免费网页层转标准 API、多账号轮转、长上下文滚动接续、内置面板——都继承自上游，不在此重复。下面只列我们在上游之上新增的东西，每条配一个实际场景：

- **回答写到一半断了，自动接上** — 例：让模型挂一个无人值守的长任务，你去忙别的；上游中途断流时，服务端在同一条回答里自动续写（最多 2 次，已生成的部分原样保留），回来时任务完整跑完——而不是答到一半悄悄断掉，整个任务被迫延误。上游遇到同样情况只能整段重发或直接报错
- **工具调用乱码进不了正文** — 例：模型偶尔吐出残缺的工具标记，16 种已知形态会在进入输出前被拦下，`<tool_call>` 之类的碎片不会漏进对话正文
- **令牌池不怕容器重建** — 例：免费平台说重建就重建，令牌表每 6 小时加密备份到仓库外、启动时发现池子空了自动恢复，不用重新逐个抓令牌
- **两条语言线** — `main` 附中文文档，[`english`](https://github.com/1515asd/rh-deeperseeker/tree/english) 线注释、日志、面板、文档全英文且不含中文，代码与 `main` 一致

## 旧对话恢复（协议变更前开始的对话）

2026-10-31 之前的旧会话，其服务端状态已被协议变更作废。历史记录**没有丢**，两种恢复方式：

1. **等待**：旧会话过期后（通常若干小时）重新发消息，客户端会自动全量重发历史，记忆自动重建
2. **等不及就去 DeepSeek 官网**：登录网页版，找到那个会话直接删除，再发消息即可重建

## 快速开始

**第一次用？先读[使用指南（中文）](docs/GUIDE.zh-CN.md)**——大白话讲清楚安装、面板、原理与常见问题。英文版见 [docs/GUIDE.md](docs/GUIDE.md)。

本地运行：

```bash
git clone https://github.com/1515asd/rh-deeperseeker.git
cd rh-deeperseeker
python3 -m venv env && source env/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填好配置再启动
python3 app.py
```

Docker / Podman：

```bash
cp .env.example .env
docker compose up -d          # 或 podman-compose up -d
```

面板地址：`http://localhost:4000/`

> 免费托管平台（Pterodactyl 系）注意：面板注入的是 `SERVER_PORT`，启动命令用 `PORT=$SERVER_PORT python app.py`；`wasm/` 目录必须完整部署；`DB_PATH` 指到持久卷可跨容器重建保留令牌池。

## 配置（`.env`）

| 变量 | 说明 | 默认值 |
|---|---|---|
| `DEEPSEEKER_API_KEY` | 访问 API 所需的 Bearer 密钥 | `dseeker` |
| `DEEPSEEKER_ADMIN_USER` | 面板登录用户名 | `admin` |
| `DEEPSEEKER_ADMIN_PASSWORD` | 面板登录密码 | `admin` |
| `HOST` | 监听地址（`127.0.0.1` 仅本机，`0.0.0.0` 对外） | `127.0.0.1` |
| `PORT` | 服务端口 | `4000` |
| `DEEPSEEKER_MAX_HISTORY_TOKENS` | 首条消息注入历史上限 | `24000` |
| `DEEPSEEKER_MAX_TOOL_RESULT_TOKENS` | 工具结果注入上限 | `12000` |
| `DEEPSEEKER_MEMORY_LIMIT_TOKENS` | 触发滚动接续的记忆上限 | `393228` |
| `DEEPSEEKER_MAX_OUTPUT_TOKENS` | 单次响应输出上限 | `8192` |
| `DEEPSEEKER_MAX_SUMMARY_TOKENS` | 交接摘要预算 | `4096` |

## 获取令牌

1. 开无痕窗口登录 `chat.deepseek.com`
2. 提取令牌（按设备二选一）：
   - **桌面 / Android（有开发者工具）**：F12 控制台执行 `JSON.parse(localStorage.getItem("userToken")).value`
   - **iPhone / iPad（Safari 书签）**：Safari 无法直接跑 `javascript:`，先收藏任意页面，编辑该书签、把网址替换为下面的代码，然后在已登录的 `chat.deepseek.com` 页面点该书签，令牌自动复制：

   ```javascript
   javascript:(function(){var t='';try{var r=localStorage.getItem('userToken');if(r){try{t=JSON.parse(r).value||r;}catch(e){t=r;}}}catch(e){alert('Read failed: '+e.message);return;}if(!t||t.length<20){alert('No userToken found.');return;}var a=document.createElement('textarea');a.value=t;a.style.cssText='position:fixed;left:-9999px';document.body.appendChild(a);a.select();a.setSelectionRange(0,999999);document.execCommand('copy');a.remove();alert('Copied. length '+t.length+'\n'+t.substring(0,12)+'...');})();
   ```

3. 把令牌粘贴进面板（`/dashboard`），关掉无痕窗口

## 令牌有效期与面板状态（人话版）

**令牌什么时候会过期？** 不固定，由 DeepSeek 服务端决定，这边无法预知。过期后客户端会报 `401`（含 `INVALID_TOKEN`）或 `503 No tokens available`，面板状态列会显示「疑似过期」。此时**重新提取一个新令牌、添加进面板、删掉旧令牌**即可恢复，点「恢复」按钮没用——令牌死了就是死了。

**面板状态是什么意思？**

| 状态 | 意思 | 你需要做什么 |
|---|---|---|
| 🟢 正常 | 能正常干活 | 什么都不用做 |
| 🔵 限速冷却 | 请求太频繁，临时休息 | 自动恢复，不用管 |
| 🟠 确认禁言 | DeepSeek 明确说账号被禁言（附解禁时间） | 解禁前发不了消息，不用管；解禁后点「恢复」即可正常使用 |
| 🟠 疑似禁言 | 账号疑似被临时限制发言 | 等解封后点「恢复」；若登录网页版能正常聊天，就是误判，直接点「恢复」 |
| 🔴 疑似过期 | 令牌失效（过期或被登出） | 重新提取令牌、添加、删旧令牌；点「恢复」无效 |
| 🔴 已隔离 | 其他原因退出轮换，说明列有具体原因 | 确认上游正常后点「恢复」；拿不准先按上面的方法自查 |

**分不清过期还是禁言？** 点面板令牌池下方的「**检测令牌**」按钮——逐个直接询问 DeepSeek 每个令牌的真实状态（正常 / 禁言到几点 / 已作废），确认禁言的会自动隔离。也可以用该账号登录 `chat.deepseek.com`：网页提示重新登录 = 过期；发消息弹出禁言提示 = 禁言；能正常聊 = 误判。另外「恢复」按钮永远是安全的——令牌真失效的话会自动再次隔离，不会造成损失。

**提示词增强开关**（面板「功能开关」区）：客户端请求里写 `reasoning_effort=max` 等档位时，给模型附加一段「慢慢想、多角度验证」的强化指令，回答更深入但稍慢、多耗 tokens。面板可随时开关，即时生效；关闭后与旧版行为完全一致。

## API 端点

- **OpenAI 兼容**：`http://localhost:4000/v1` — `/v1/chat/completions`（流式/非流式）、`/v1/responses`、`/v1/models`、`/v1/files`
- **Anthropic 兼容**：`http://localhost:4000` — `/v1/messages`、`/v1/files/upload`
- **模型**：`v4.1flash`（唯一默认模型，原生支持视觉）。客户端传什么 `model` 都会归一化到它；`/v1/models` 里同时列出 `anthropic/claude-v4.1flash` 供 Claude Desktop 自动发现

## 免责声明

⚠️ 自动化使用违反 DeepSeek 的服务条款。本项目与 DeepSeek 无任何关联。请使用专用小号，切勿使用个人主号；账号可能随时被封禁，风险自负。请勿滥用，仅限个人用途。

## 许可

MIT — 见 [LICENSE](LICENSE)。版权归上游作者 AmanCode22，本 fork 的改进以同一许可发布。
