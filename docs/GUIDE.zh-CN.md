# DeeperSeeker 使用指南（中文版）

> 术语我们都会给两个版本：**先说人话，再给术语**。懂的人看术语就够了，不懂的人看人话就能跟着做。

---

## 一、这是个什么东西？

**人话版**：你在自己电脑（或服务器）上跑一个程序，它帮你把「DeepSeek 网页版聊天」变成一个正规的 AI 接口。以后你的各种 AI 工具（写代码的 Claude Code、聊天面板 Cherry Studio、Open WebUI 等等）都能直接连它用，就像用付费官方 API 一样——但用的是 DeepSeek 网页版的免费额度。

**术语版**：DeeperSeeker 是一个自托管（self-hosted）的**反向代理网关**。它持有你的 DeepSeek 网页会话凭据（token），对外暴露标准的 OpenAI Chat Completions（`/v1/chat/completions`）与 Anthropic Messages（`/v1/messages`）端点，内部负责会话映射、PoW 挑战求解、多账号轮转、上下文滚动压缩等全部脏活。

一句话：**网页版白嫖额度 + 官方 API 的接入体验。**

---

## 二、5 分钟跑起来

### 1. 装依赖并启动

```bash
git clone https://github.com/1515asd/deeperseekerSncMax.git
cd deeperseekerSncMax
python3 -m venv deeperseeker_env
source deeperseeker_env/bin/activate        # Windows: deeperseeker_env\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                        # 没有 .env.example 就跳过，见下
python3 app.py
```

**人话**：`.env` 就是这个程序的「设置文件」，第一次用只需要改三样：面板登录密码、API 访问密钥、监听地址。全部配置项见主 README 的 Configuration 章节。

**术语**：`.env` 承载环境变量（DEEPSEEKER_ADMIN_PASSWORD、DEEPSEEKER_API_KEY、HOST/PORT 等），由启动时加载。

### 2. 登录面板

浏览器打开 `http://你的地址:4000/`，用 `.env` 里设置的用户名密码登录（默认 admin/admin，**改掉它**，尤其服务绑在公网时——启动日志会反复提醒你）。

### 3. 添加你的第一个 DeepSeek 账号

**人话**：程序需要知道用哪个 DeepSeek 账号去聊天。这个「账号凭证」叫令牌（token），从浏览器里提取，面板首页有完整图文教程（电脑 F12 一行命令 / iPhone 快捷指令 / Safari 书签三种方法，选一个就行）。

**术语**：token 是 DeepSeek 网页版存储在 localStorage 的会话 JWT（`userToken`）。服务端持有它即可代替浏览器与会话后端通信。

面板上点「添加令牌」→ 粘贴 → 完成。多加几个账号更好——程序会自动在它们之间轮流用（**术语**：多账号池轮转，rate limit 时自动切换）。

### 4. 接上你的 AI 工具

以 OpenAI 兼容为例（绝大多数工具都是这个）：

- **接口地址（Base URL）**：`http://你的地址:4000/v1`
- **API Key**：`.env` 里你设置的 `DEEPSEEKER_API_KEY`
- **模型名**：`v4.1flash`（唯一默认模型，客户端传什么都会归一化到它）

Anthropic 兼容客户端（如 Claude Code）用 Base URL `http://你的地址:4000` 即可，其余同上。

---

## 三、启用页面模式（Snc1，可选）

**人话**：照第二节装完，默认跑的是老办法——服务端自己组装请求发给官方。首页说的那套「让官方页面自己发」，要额外做三件事：开一个开关、在浏览器装一个小脚本、让那个页面保持登录。三样缺一样，发消息就会一直等到超时然后报错，**不会**自动退回老办法。

**术语**：这条路径叫 page mode（页面模式）。服务端只拼一个带 `q=` 与 `autosend=1` 的网址挂进待发队列，不再构造 completion 请求；配套的用户脚本 `page_mode_carrier.user.js` 从 `/page_mode/pending` 领取网址，在已登录的官方页面里用 `location.href` 加载它，由页面自己把消息发出去；服务端再用只读接口 `fetch_page` / `history_messages` 把回复读回来。

### 1. 打开开关

编辑 `.env`，加一行后重启服务：

```bash
DEEPSEEKER_PAGE_MODE=1
```

开关只认 `1` / `true` / `yes` / `on`，`.env.example` 里有一段注释模板。删掉这行或不设，就是老办法，其余功能一点不受影响。想调等待时长可加 `DEEPSEEKER_PAGE_TIMEOUT=300`（秒，默认 300）。

### 2. 在浏览器装配套脚本

`page_mode_carrier.user.js` 是用户脚本，用 Tampermonkey / Violentmonkey 之类扩展安装，只在 `https://chat.deepseek.com/*` 下运行。装之前把脚本开头两行的默认值改成你自己的：

```js
const BRIDGE = GM_getValue("BRIDGE", "http://127.0.0.1:4000");  // 服务在别的机器就换成那台的地址
const KEY    = GM_getValue("KEY",    "dseeker");                 // 填 .env 里的 DEEPSEEKER_API_KEY
```

`KEY` 也可以在官方页面的控制台执行 `localStorage.setItem("ds_page_mode_key", "你的key")` 交给脚本兜底读取。别把真实 API Key 提交到公开仓库。

### 3. 保持页面登录同一个账号

浏览器打开 `https://chat.deepseek.com/`，登录**和面板里那个令牌同一个**账号，标签页保持开着（后台也行）。脚本每 4 秒来取一次待发网址，取到就跳转过去发送。浏览器登录的账号和面板令牌不是同一个，会读不到回复、一直等到超时。

### 4. 验证是否生效

```bash
curl -H "Authorization: Bearer 你的KEY" http://127.0.0.1:4000/page_mode/pending
```

| 返回 | 含义 |
|---|---|
| `404 {"error":"page mode disabled"}` | 开关没开 |
| `200 {"pending":[]}` | 已开启，正常 |
| `401 {"error":"Invalid API key"}` | 脚本里的 `KEY` 填错了 |

然后随便发一条消息，看浏览器那个标签页是否自动跳转并发出。

### 用之前要知道的几条

- **不带附件**：图片 / 文件请求会明确返回 400，页面模式只承载文字；要传附件就关掉开关走老办法。
- **单条正文上限 800 字符**：超了会降级成只发最后一条用户消息，再超才返回 400，不会静默截断。
- **失败不静默回退**：脚本没装、页面没登录、账号不一致、标签页关了，都会等到超时后明确报错，不会偷偷切回老办法。
- **多轮记忆交给页面**：这条路径下上下文由官方页面自己的会话承担，服务端不再做会话映射与摘要滚动。

想退回老办法：删掉 `DEEPSEEKER_PAGE_MODE=1`、重启，别的都不用动。

---

## 四、面板功能速览

新面板是一个页面搞定全部运维：

| 区域 | 人话 | 术语 |
|---|---|---|
| 顶部六张卡片 | 一眼看到：几个号能用、几个号总数、正在处理几个请求、今天用了多少 | 活跃令牌数 / 令牌池容量 / in-flight 并发 / 今日请求与 tokens 聚合 / 会话映射行数 |
| 上游健康 | 「DeepSeek 那边现在通不通？」——点「立即探测」或等它每 30 秒自动测一次 | 深度健康探针：真实端到端往返（含 PoW 挑战求解），与 `/health?deep=1` 同路径 |
| 令牌池表 | 每个账号什么状态、在干嘛、冷却还剩多久，坏的可以手动「恢复」 | 池运维视图：状态机（ACTIVE/RATE_LIMITED/QUARANTINED）、in-flight 计数、冷却计时、隔离恢复 |
| 用量统计 | 哪天、哪个 key、用了多少 | 按天 × 掩码 key 聚合的 usage 表 |

数据每 10 秒自动刷新，不用手动按 F5。浅色/深色跟随系统。

---

## 五、它是怎么工作的（原理，想深入再看）

**人话版**：

1. 你的工具发来一段对话 → 程序查「对号表」看这段对话之前是不是聊过 → 聊过就续着老会话发，没聊过就开个新会话把全部历史打包发过去。
2. 每轮回复 DeepSeek 都会广播一个「消息编号」，程序记住它，下一轮接着编号往下聊——不猜，编号错了上游会拒绝。
3. 对话太长快装不下时，程序会自动把旧内容总结成一段「前情提要」，开个新会话接着聊——你这边无感知，模型也不失忆。
4. 账号被限速了？自动换下一个账号顶上，聊天的上下文跟着一起搬过去。
5. 上游偶尔抽风（比如模型说到一半停了），程序会自动续写，最多重试两次。

**术语版**：

1. **会话映射（session mapping）**：对客户端消息历史做规范化 SHA-256 签名，命中则复用上游会话（增量发送），未命中走首条全量注入路径（full-history injection）。
2. **真实编号对接（real message ID）**：2026-10-31 起 DeepSeek 在流首广播 `response_message_id`，服务端捕获后作为下一轮 `parent_message_id`，全链路零猜测（旧的 +2 递增已移除）。
3. **上下文滚动压缩（summarize-and-rollover）**：逼近记忆上限（约 393K tokens）时触发摘要换血，滚动摘要（rolling summary）+ 最近原文尾巴（tail）+ 最新消息组成新会话首条。
4. **故障转移（failover）**：401/403/429 自动隔离当前令牌、新令牌接管并完整迁移上下文。
5. **空流自动续写（empty-stream auto-retry）**：上游 502/静默零输出按中断处理自动续写 ≤2 次。

---

## 六、常见问题

### Q：升级后老对话「失忆」了？

**人话**：2026-10-31 官方改了规矩，那天之前的老对话需要一次「重新认识」。两种恢复方式：
1. **等**：等老会话过期后重新发消息，你的客户端本来每次都带完整历史，发一条就全想起来了；
2. **等不及**：去 DeepSeek 官网登录 → 找到那个会话 → **直接删掉** → 回来重新发消息就能新建会话。

**术语**：协议变更窗口遗留的会话映射携带过期 `parent_message_id`，上游以 biz_code 26 拒收。服务端脏会话自愈（poisoned-session rebuild）会在失败时删除映射并按首条重建；也可手动清理上游会话加速恢复。

### Q：模型说到一半就停了？

现版本会自动续写。如果还遇到，大概率是上游瞬时抖动，重发即可；同一条消息重发不会重复执行（幂等回放窗口保护的是**成功**回合，失败回合照常重试）。

### Q：工具调用（function calling）可靠吗？

可靠。服务端支持多格式工具调用解析，并带泄露闸门（畸形工具标记不会漏进正文）与参数修复。写 HTML/XML 内容作为参数值也不会被破坏。

### Q：账号被封了怎么办？

用小号。被封的账号会自动隔离出池，不影响其他账号继续服务（failover）。**永远不要用主力账号。**

---

## 七、安全须知（重要）

- 面板密码一定要改（默认 admin/admin）。
- `DEEPSEEKER_API_KEY` 是你对外服务的钥匙，别用弱值。
- 令牌 = 账号本体，不要发给任何人、不要贴进别人的面板。
- 自动化使用违反 DeepSeek 服务条款，封号风险自担；请勿搭建公共转发站。
- 建议只绑定本机（HOST=127.0.0.1），需要远程访问时套 VPN/反向代理 + HTTPS。

---

有问题去仓库提 [Issue](https://github.com/1515asd/deeperseekerSncMax/issues)（英文/中文都可以）。英文版指南见 [GUIDE.md](GUIDE.md)。
