# Long Run Hybrid Coach

[繁體中文](README.md) · [English](README.en.md) · **简体中文**

官网 [paceandstaystrong.com](https://paceandstaystrong.com/zh/) ｜ 遇到问题看[支持页](https://paceandstaystrong.com/zh/support.html)（繁体）

把一个网址贴进 Claude 或 ChatGPT，你就有一个读得到你真实训练的教练。

它读你 Intervals.icu 账号里的活动与恢复数据，维持同一份 28 天的跑步＋力量训练方向与本周课表，拿计划去对你实际做了什么，并在你同意之后，把每一堂课排进你的日历。**免费使用，没有付费方案。**

**Garmin 不是使用前提。** Garmin 只是目前第一条做过实机验证的下游设备路径；Apple Watch、COROS、Polar、Suunto、Wahoo、其他 app／手表，甚至没有手表，都可以用同一个教练。差别在于有多少可信的训练记录能进到教练手上，以及 Intervals 后面那段设备同步是否已经被验证过。

> **一般用户直接用托管版：** `https://mcp.paceandstaystrong.com/mcp`。你需要一个 Intervals.icu 账号，但不需要自己向 Intervals 申请 OAuth App，也不需要自己运维服务器。

---

## 连接：两步

官网把同一条流程拆成四个点击步骤走一次：[开始使用](https://paceandstaystrong.com/zh/#setup)。

### 你需要什么

1. **一个 Intervals.icu 账号。** 免费，用 Google 登录大约 30 秒就能开好。
2. **Claude 或 ChatGPT。**
   - **Claude**（claude.ai／Claude Desktop）：免费方案就可以，免费账号限一个自定义连接器。这条路径已经做过完整实机验证——production OAuth、教练对话、以及把课表送进 Intervals。
   - **ChatGPT**：目前需要 Business、Enterprise 或 Edu 的网页版 workspace，在 Apps／developer mode 建立 custom app。个人方案的 custom MCP 只有 read/fetch，跑不完本产品「写计划＋交付课表」的流程；个人方案请先等这个教练在 ChatGPT 目录上架。最新限制以 [OpenAI 官方说明](https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt) 为准。
   - **其他 MCP client**：OpenClaw 与自建 client 见[下面一节](#其他-mcp-client)。
3. **可选**：已经会把活动同步进 Intervals.icu 的手表或训练 app。没有手表也可以用——教练会把拿不到的字段当成「不知道」，不会当成 0。

### 第一步：把网址贴进你的 AI

在 AI 的连接器设置里新增一个 remote MCP server，网址是：

```text
https://mcp.paceandstaystrong.com/mcp
```

- **claude.ai／Claude Desktop**：Settings → Connectors → Add custom connector → 贴上网址。
- **ChatGPT**：Apps／developer mode → 建立 custom app → 指到同一个网址。

没有别的字段要填，也没有密钥要贴。

### 第二步：授权 Intervals.icu

浏览器会打开 Intervals.icu 的同意页。登录**你自己的 Intervals.icu 账号**，四个权限都勾上：

| 权限 | 教练拿它做什么 |
| --- | --- |
| `ACTIVITY:READ` | 读你已完成的训练。 |
| `WELLNESS:READ` | 读 Intervals 手上的恢复数据。 |
| `CALENDAR:WRITE` | 读写训练日历，让你确认过的课表能送进去，并读回来核对。 |
| `SETTINGS:WRITE` | 读你的阈值配速。它唯一会写进去的，是你**还没有**的阈值配速，发生在你已经确认过的课表交付当下；已经设好的绝不覆写。 |

Intervals 也有只读的设置权限；这里要写入，是因为上面那个补值动作真的会写。它之所以需要，是因为 Intervals 这边没有阈值配速时，它照样收下带配速的课表，但往手表送的时候会把配速目标拿掉——你会收到距离正确、却没有任何目标的一堂课。少勾任何一个权限，需要它的功能就会坏掉，而且不容易看出原因；重新连接补上即可。

**不要把 Intervals 密码、API key 或 token 贴进对话。**

### Intervals 账号是新的或空的？

教练读的是 Intervals.icu 里已经有的东西。账号是新的话，有两条路把历史补进去：

- **接上你本来就在用的设备或 app。** 在 Intervals.icu 自己的 Settings 里连 Garmin 或你的手表／训练 app，过去的活动会自动补上。数据到了之后，再请教练重新读一次。
- **把导出文件直接交给教练。** 在对话里给它 CSV、Apple Health 导出文件或 `.fit` 文件，请它导入。同一个文件与同一场活动会自动去重，判断不了才回头问你。（文件是给教练的，不会进 Intervals.icu。）

设备量不到的东西也可以直接在对话里讲：力量训练的实际组数重量次数、本周能练的时间与器材、体重体脂、没戴表的那一场、「最近很累」「睡不好」，以及你从手表上实际看到的睡眠、HRV、静息心率、readiness 数字。教练不会把一句「我很累」偷偷翻成一个假的 readiness 分数。

---

## 连上之后会发生什么

### 直接问正常的教练问题

不用先填问卷：

```text
读我最近的训练，告诉我这周该怎么练。
```

或：

```text
我想提升 VO2max，又不想掉力量，帮我排第一个 28 天方向。
```

教练会先读已经有的数据，再只问真正会改变决策的缺口——例如本周可练日、器材，或设备不可能知道的力量训练基准。

### 第一次会先看 28 天预览，你同意才写进去

- **本周**：精确、可执行、可以直接送进日历的课。
- **后三周**：大方向，不假装现在就知道所有细节。

之后每一次改动也是同一条体验：**改动前后对照 → 你同意一次 → 才写入**。

### 要送进日历时，再确认一次

交付是另一个独立确认：**课表预览 → 你同意一次 → 写进 Intervals.icu → 读回来核对**。

本产品能证明的最远一步是 Intervals.icu 收下了。**Intervals 成功不等于课表已经在 Garmin、Apple Watch 或其他手表上**——Intervals 后面那段同步是外部路径，要各自验证。

---

## Intervals.icu 在这个产品里做什么？

Intervals.icu 是目前的**中转站**：它帮教练接住不同设备／app 的活动与恢复数据，也承接教练确认后的日历课表。它**不是计划本身的存放处**。

```text
手表 / 训练 app
      │
      ▼
 Intervals.icu ───── 已完成活动 + 恢复数据 ─────► 教练
      ▲                                          │
      │                                          │
      └────────── 你确认过的日历课表 ◄───────────┘
      │
      ▼
Garmin / Apple Watch / 其他下游同步
```

责任分工：

- **Intervals.icu**：整合外部训练数据，并持有日历。
- **Long Run Hybrid Coach**：持有唯一的当前计划、决策历史、你自己上报的记录、每一次确认的绑定，以及整条教练流程。
- **你的手表／app**：可以把活动带进 Intervals，也可能接收 Intervals 往下送的课表；但最后一公里是否成功，要独立验证。

只有**账号本身**是必要条件。Intervals 里已经有活动与恢复数据的话，教练自动拿得到的数据会比较完整；没有的字段保持「不知道」，不会被当成 0，也不会因为少一个可选数值就把一般教练对话挡掉。

---

## 托管版 vs 自建

实际会感觉到的差别只有一个：**托管版你在手机上就能直接用；自建只有在跑服务器的那台电脑上能用。**下面每一行都是这个差别的成本。

| | 托管版（推荐） | 自建 |
| --- | --- | --- |
| 手机上能用吗 | 能——连一个有手机 App 的 client 就好 | 不能，除非你自己把服务器对外开放并处理 TLS |
| 网址 | `https://mcp.paceandstaystrong.com/mcp` | 你自己的服务器，例如 `http://127.0.0.1:8422/mcp` |
| 运维 | 不用自己管 | 自己启动、更新、备份与运维 |
| Intervals OAuth App | **不需要** | **需要**自己的 OAuth application 凭证 |
| 计划存哪 | 托管端、以每位用户为范围 | 你自己的服务器 state root |
| 适合谁 | 一般用户、多个 client 共用同一份计划 | 开发者、需要完全自管环境／数据的人 |

托管版会自己处理 dynamic client registration、PKCE、token 与用户映射；一般用户不需要任何 id、API key、client secret 或环境变量。

### 其他 MCP client

- **OpenClaw**：用 `openclaw mcp add` 指到同一个网址，加上 `--auth oauth`。一个 instance 若不只一个人用，要把 OAuth identity 设成 per-requester，否则所有人会连到同一个 Intervals 账号。配置见 [entrypoints/openclaw/](entrypoints/openclaw/README.md)。
- **其他**：把同一个网址配置成 remote Streamable HTTP MCP server。跑在你自己机器上的 client（OAuth callback 落在 loopback）可以直接连；跑在云主机、用自己域名接 callback 的 client，注册会被拒绝，需要先把该 origin 加进部署的信任列表。细节见 [entrypoints/mcp/README.md](entrypoints/mcp/README.md)。

逐入口「已完整实机验证」或「已封装、等待真实连接验证」的状态，以 [entrypoints/](entrypoints/README.md) 为准。

### 自建怎么跑？

Repo 使用 Python 3.11，产品本身只用标准库，不需要先安装一串依赖包。

1. Clone repo。
2. **向 Intervals.icu 申请建立 OAuth application。** Intervals 目前的公开流程不是在 Settings 自助新增：按官方说明提供 app name、description、website、logo、privacy policy、redirect URI 与你的 Intervals ID；app 建立后才会出现在 Settings，从 **Manage App** 取得 `client_id` / secret。流程见 [Intervals.icu OAuth support](https://forum.intervals.icu/t/intervals-icu-oauth-support/2759)。
3. 在 Intervals app 里注册 callback：`<gateway-origin>/oauth/callback`。本机 client 可以走 loopback；remote client 需要可达的 HTTPS 或安全隧道。
4. 配置必要环境变量：

```bash
export GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT="$HOME/.local/share/long-run-hybrid-coach-gateway"
export GARMIN_COACH_LOOP_TOKEN_HMAC_KEY="$(openssl rand -base64 32)"
export GARMIN_COACH_LOOP_INTERVALS_CLIENT_ID="..."
export GARMIN_COACH_LOOP_INTERVALS_CLIENT_SECRET="..."
```

5. 启动：

```bash
python3 -m garmin_coach_loop.cli serve-gateway --host 127.0.0.1 --port 8422
```

6. 本机 MCP client 指到 `http://127.0.0.1:8422/mcp`。

要正式提供给 remote client 的话，不要把 loopback 示例当 production runbook。Persistent volume、TLS、信任的 client origin、single replica、release identity 与部署验证见 [docs/deploy-gateway.md](docs/deploy-gateway.md)。

### 一个人只该有一份当前计划

本机设置 `GARMIN_COACH_LOOP_GATEWAY_URL` 指向托管版时，本机写入默认会被挡；只有明确加 `--offline` 才代表「我刻意在做另一份本机计划」。已经有本机数据的人可以搬到托管版，流程见 [docs/ops/migrate-local-store-to-hosted.md](docs/ops/migrate-local-store-to-hosted.md)。

---

## 现在可以做什么？

- 维护一份 **28 天方向**：本周精确的课 ＋ 后三周大方向。
- 读 Intervals 的活动、恢复数据与日历，把可信的「排了什么 vs 实际做了什么」自动对回当前计划。
- 在同一份周计划里同时处理跑步与力量训练。
- 记录你自己上报的东西：个人资料、可练时间、长期目标、训练习惯、实际力量训练、体重体脂、设备没记录到的活动，以及主观状态。
- 每次对话可以带入当下的恢复数字。托管版不会、也不需要去读你电脑上的健康数据库。
- 导入历史数据：支持格式的 CSV、Apple Health XML，以及 FIT 文件。同一个文件与同一场活动会自动去重，判断不了才问你。
- 课表可以带一段教练备注一起进 Intervals，而不是偷偷长出第二套课表写法。
- 每周复盘「实际练了什么、有没有进步的证据、下一步是什么」，而不是把「课表做完」直接当成体能提升。
- 计划变更先看对照，同意后才应用。
- 日历交付先看预览，同意后才写入；支持安全重试、替换与撤回本产品自己送出去的课表。
- 在对话里直接导出，或分两段永久删除本产品持有的数据。

### 重要边界

- 开始一次教练对话会做自动对账，**可能写入新的计划版本**。只想看目前存了什么、完全不动数据的话，用只读的那条路径（`getCoachState`）。
- 你自己上报的活动是佐证数据，不会被偷偷升格成设备确认的完成记录。
- 恢复数字只接受真的观察值；模型不可以从文字自己猜一个数字出来。
- 本产品不做医疗诊断。
- 交付的证据只到 Intervals 读回来核对，不会声称已经到手表。

---

## 数据、导出与删除

托管端保存维持同一份计划所必要的东西：计划的版本链、决策与回执、你自己上报的记录、身份映射，以及还没收敛的交付记录。

导出时刻意**不包含**三样东西：授权凭证的 fingerprint（单向的指纹，只拿来做内部记账）、供应商的原始 payload 与 GPS 轨迹（原始活动文件应该跟供应商拿），以及内部的 owner id（本产品自己的存储位置编号）。

删除也有三个明确边界，这三件不在本产品能删的范围：

- 已经写进 **Intervals.icu 日历**的课表；
- 你在 **Intervals.icu Settings** 给出的授权；
- 不含计划、健康或身份内容的最小化平台**运营日志**。

完整生命周期见 [docs/account-lifecycle.md](docs/account-lifecycle.md)，公开隐私政策在 [paceandstaystrong.com/zh/privacy.html](https://paceandstaystrong.com/zh/privacy.html)（繁体；以英文版为准）。

---

## 目前限制

- 教练不直接登录 Apple Health、Garmin Connect 或其他设备账号；主要的自动数据路径目前仍是 Intervals.icu。
- 托管版不会永久保存每次传入的恢复数字；下一次需要就再讲一次当下的数值。
- 本产品不观察 Intervals 之后的每一段设备同步，因此不会把「Intervals 收下了」说成「已经在手表上」。
- 自建是给开发者／自管的人；一般用户应优先用托管版。
- 设备兼容性是逐条路径的证据，不会因为 Garmin 已验证就推论其他设备一定相同。
- Remote client 的 OAuth callback origin 不是开放注册：loopback 一律可用，claude.ai／claude.com／chatgpt.com 内置信任，其他云主机上的 client 要先由运维者加进信任列表。
- 这是一个人的项目，不是公司。不保证服务不中断，也可能改变。

---

## 遇到问题

- **[支持页](https://paceandstaystrong.com/zh/support.html)**（繁体）：导出、删除、更正、撤销授权——大部分事情你在对话里自己就能做完，而且比等人回复快。上面也有直接寄给开发者的邮箱。
- **[Issue tracker](https://github.com/atomchung/long-run-hybrid-coach/issues)**：bug 与功能建议。它是公开且永久的，**不要贴**健康／训练／计划内容、Intervals 的 athlete id、token 或任何凭证。要指认自己的账号，用你数据导出文件里那个不可还原的引用码就够了。
- 认为是安全或隐私漏洞的话，不要公开描述，直接发邮件。

---

## 技术文档

- 稳定用户故事：[docs/user-story.md](docs/user-story.md)
- 用户路径与对应的调用：[docs/user-flows.md](docs/user-flows.md)
- 数据来源与字段边界：[docs/data-sources.md](docs/data-sources.md)
- 入口与平台配置：[entrypoints/](entrypoints/README.md)
- MCP protocol、OAuth 与 tool 行为：[entrypoints/mcp/README.md](entrypoints/mcp/README.md)
- 托管部署：[docs/deploy-gateway.md](docs/deploy-gateway.md)
- 账号生命周期：[docs/account-lifecycle.md](docs/account-lifecycle.md)
- 公开上架／reviewer 材料：[docs/distribution/](docs/distribution/README.md)
- Release inventory：[docs/release-inventory.md](docs/release-inventory.md)
- Repository invariants 与验证：[AGENTS.md](AGENTS.md)

目前 release 对外有 **22 个 MCP tool**、**2 个 prompt**、**31 个 CLI 指令**、**4 份 JSON Schema contract**、**9 张 identity 表**。这些数量由测试从真实代码推导，避免这份文档自己走样。

Long Run Hybrid Coach 是独立项目，与 Garmin、Intervals.icu、Apple 或其他设备／平台供应商没有隶属、背书或赞助关系。代码以 [MIT License](LICENSE) 发布。
