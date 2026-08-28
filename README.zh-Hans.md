# Long Run Hybrid Coach

[繁體中文](README.md) · [English](README.en.md) · **简体中文**

Long Run Hybrid Coach 是一个非官方、Intervals-first、device-agnostic 的个性化 hybrid training coach。它维护同一份 28 天方向和本周跑步＋力量训练计划，读取可信的实际完成 evidence 持续复盘，并可在你确认后把训练计划送到 Intervals.icu 日历。

**Garmin 不是使用前提。** Garmin 是目前第一条做过实机 dogfood 的下游设备路径；Apple Watch、COROS、Polar、Suunto、Wahoo、其他 app／手表，甚至没有手表，都可以使用同一个 Coach。差异在于有多少可信 evidence 能进入训练循环，以及 Intervals 后面的设备同步路径是否已经单独验证。

> **普通用户优先选择 Hosted MCP：** `https://mcp.paceandstaystrong.com/mcp`。你需要一个 Intervals.icu 账号，但不需要自己创建 Intervals OAuth App，也不需要自己维护 gateway。

---

## Quick Start：Hosted MCP

### 使用前需要什么？

1. 一个 **Intervals.icu 账号**。
2. 一个能连接 remote MCP、并且能提供本产品所需动作的 AI client。
3. 可选：已经会把活动同步进 Intervals.icu 的手表或训练 app。

没有 Garmin 也可以使用。缺少可选 recovery evidence 时，Coach 会保持 unknown，而不是当成 0。

### 1. 连接 Hosted Coach

MCP endpoint：

```text
https://mcp.paceandstaystrong.com/mcp
```

- **claude.ai / Claude Desktop**：Settings → Connectors → Add custom connector → 填入 endpoint。这条路径已经完成 production OAuth、coaching turn 与 Intervals delivery 的实机验证。
- **ChatGPT**：按照 OpenAI 当前官方说明，包含 write／modify actions 的完整 MCP 目前以 beta 形式提供给 ChatGPT Business、Enterprise 与 Edu 网页版；Pro 的 custom MCP 当前只有 read/fetch，无法完成本 Coach 的 plan write／delivery 全流程。如果你的 workspace 支持完整 MCP，可在 Apps／developer mode 建立 custom app 并指向上面的 remote endpoint。最新限制以 [OpenAI 官方说明](https://help.openai.com/zh-hans-cn/articles/12584461) 为准。
- **OpenClaw**：用 `openclaw mcp add` 指到同一个 endpoint，并加上 `--auth oauth`；一个 instance 若不只一个人用，要把 OAuth identity 设成 per-requester，否则所有人会连到同一个 Intervals 账号。配置见 [entrypoints/openclaw/](entrypoints/openclaw/README.md)。
- **其他 MCP client**：把同一个 URL 配置为 remote Streamable HTTP MCP server；是否能跑完整流程取决于该 client 的 MCP/OAuth 能力。

各入口目前是“已实机完整验证”还是“已封装、等待真实连接验证”，以 [entrypoints/](entrypoints/README.md) 为准。

### 2. 授权 Intervals.icu

第一次连接时，浏览器会打开 Intervals.icu 同意页面。登录**你自己的 Intervals.icu 账号**并授权 Coach 需要的能力：

- `ACTIVITY:READ`：读取已完成训练。
- `WELLNESS:READ`：读取 Intervals 可提供的 wellness evidence。
- `CALENDAR:WRITE`：读／写训练日历，让确认过的训练计划可以交付并 read-back 验证。
- `SETTINGS:WRITE`：读取设置；只有在已确认的 delivery 流程确实需要时，才补上缺失且有 evidence 支持的 running threshold setting。

Intervals 同意页会把权限分开。少勾一项时，依赖该权限的能力会明确失败；重新连接并补上权限即可。**不要把 Intervals 密码、API key 或 token 贴进对话。**

### 3. 直接问正常的教练问题

不需要先填问卷，例如：

```text
读我最近的训练，告诉我这周应该怎么练。
```

或：

```text
我想提升 VO2max，又不想掉力量，帮我排第一个 28 天方向。
```

Coach 会先读取已经存在的 evidence，再只询问真正会改变决策的缺口，例如本周可训练日期、器材，或 provider 不可能知道的力量训练 baseline。

### 4. 先看 28 天 preview，再确认计划

第一次建立计划时会先看到：

- **本周**：精确、可执行、可交付的 session。
- **后三周**：方向性 outlook，不假装现在就知道全部细节。

你确认这份 preview 后，计划才会写入。之后每周改动也是同样体验：

**before / after preview → 一次确认 → apply**。

### 5. 要送进日历时，再做 delivery 确认

交付是另一个独立确认：

**delivery preview → 一次确认 → 写入 Intervals.icu → read-back 验证**。

本产品能证明的最远状态是 `intervals_accepted`。**Intervals 成功不等于训练计划已经在 Garmin、Apple Watch 或其他手表上。** Intervals 后面的同步属于外部 hop，需要按设备路径分别验证。

---

## Intervals.icu 在这个产品里做什么？

Intervals.icu 是目前的 **interoperability hub**：它帮助 Coach 接收不同设备／app 的活动与 wellness evidence，也承接 Coach 确认后的日历训练计划。它**不是 Coach 的 PlanState source of truth**。

```text
手表 / 训练 app
      │
      ▼
 Intervals.icu ───── 已完成活动 + wellness evidence ─────► Coach
      ▲                                                   │
      │                                                   │
      └────────── 确认后的 calendar workout ◄────────────┘
      │
      ▼
Garmin / Apple Watch bridge / 其他下游同步
```

职责分工：

- **Intervals.icu**：整合外部训练 evidence，并持有 provider calendar。
- **Long Run Hybrid Coach**：持有唯一 current PlanState、decision history、athlete-reported evidence、确认 binding 与 coaching workflow。
- **你的手表／app**：可以把活动带进 Intervals，也可能接收 Intervals 往下发送的 workout；但最后一跳是否成功，是独立 compatibility evidence。

只有**账号本身**是必要条件。有活动／wellness 已经同步进去，Coach 的自动 evidence 会更完整；没有的字段保持 unknown，不会被当成 0。

设备量不到或没有同步的内容可以直接在对话里告诉 Coach，例如实际力量训练组数／重量／次数、可训练时间、器材、体重／体脂、没带表的一次活动、subjective state，以及你从设备实际看到的 sleep、HRV、resting HR、readiness 等 recovery reading。

Coach 不会把一句“我很累”偷偷转换成一个假的 readiness score。

---

## Hosted MCP vs Local / Self-hosted MCP

实际会感觉到的差别只有一个：**hosted 你在手机上就能直接用；local 只有在跑 gateway 的那台电脑上能用。**下面其他每一行，都是这个差别的成本。

| | Hosted MCP（推荐） | Local / Self-hosted MCP |
| --- | --- | --- |
| 手机上能用吗 | 能——连一个有手机 App 的 client 就好 | 不能，除非你自己把 gateway 对外开放并处理 TLS |
| MCP URL | `https://mcp.paceandstaystrong.com/mcp` | 你自己的 gateway，例如 `http://127.0.0.1:8422/mcp` |
| 运维 | 不用自己管 server | 自己启动、更新、备份与维护 |
| Intervals OAuth App | **不需要** | **需要**自己的 OAuth application credential |
| current plan 存在哪里 | hosted per-athlete owner store | 你自己的 gateway state root |
| 适合谁 | 普通用户、多 client 共用同一计划 | 开发者、需要完全自管环境／数据的人 |

### Hosted MCP 最短启用流程

1. 在支持完整需求的 MCP client 新增 remote MCP app／connector。
2. URL 填 `https://mcp.paceandstaystrong.com/mcp`。
3. 完成 client OAuth 流程。
4. 浏览器进入 Intervals.icu 同意授权。
5. 回到聊天后直接问第一个教练问题。

Hosted 服务会自己处理 dynamic client registration、PKCE、gateway token 与 per-athlete owner mapping；普通用户不需要 owner id、athlete id、API key、Intervals client secret 或 server environment variable。

### Local / Self-hosted MCP 怎么跑？

Repo 使用 Python 3.11，产品本身是 stdlib-only，不需要先安装一串 runtime Python package。

1. Clone repo。
2. **向 Intervals.icu 申请创建 OAuth application。** Intervals 当前公开流程不是在 Settings 自助新增 app：按照 [Intervals.icu OAuth support](https://forum.intervals.icu/t/intervals-icu-oauth-support/2759) 提供 app name、description、website、logo、privacy policy、redirect URI 与你的 Intervals ID；app 创建后才会出现在 Settings，从 **Manage App** 获取 `client_id` / secret。
3. 在 Intervals app 中注册 gateway provider callback：`<gateway-origin>/oauth/callback`。本机 client 可以走 loopback；remote client 需要可访问的 HTTPS／secure tunnel。
4. 设置 gateway 必要环境变量：

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

6. 本机 MCP client 指向：

```text
http://127.0.0.1:8422/mcp
```

如果要正式提供给 remote client，请按照 [docs/deploy-gateway.md](docs/deploy-gateway.md) 处理 persistent volume、TLS、trusted client origin、single replica、release identity 与部署验证。

一个 athlete 应该只有一个 current writer。当 `GARMIN_COACH_LOOP_GATEWAY_URL` 指向 hosted coach 时，本机 store 写入默认会被阻止；只有明确加 `--offline` 才代表另一份 local plan。已有本机 state 可按 [docs/ops/migrate-local-store-to-hosted.md](docs/ops/migrate-local-store-to-hosted.md) 迁移。

---

## 当前能力与边界

Coach 目前可以维护一份 28 天方向；reconcile 可信 planned → actual；把跑步与力量训练放在同一周；记录 athlete-reported profile、availability、goal、preference、strength execution、body measurement、未记录活动与 subjective state；接受 request-scoped recovery readings；导入支持的 CSV、Apple Health XML 与 FIT 历史并做 deterministic 去重；把 `coach_note` 带进交付 session；做每周复盘；preview／确认计划变更；以及 preview／确认／重试／replace／withdraw product-owned calendar delivery。

重要边界：

- `startCoachSession` 会做 deterministic reconciliation，**可能写入新的 PlanState version**；只需要完全 read-only 的 stored state 时使用 `getCoachState`。
- athlete-reported activity 是 evidence，不会被偷偷升级成 provider-backed actual completion。
- recovery 数字必须来自实际观察，不能由模型从文字猜测。
- 本产品不做医疗诊断。
- Delivery 证据只到 Intervals read-back。

---

## 数据、导出与删除

Hosted 保存维持同一 owner 计划所需要的产品状态：PlanState version chain、decision／receipt、athlete-reported evidence、identity mapping，以及未收敛 delivery bookkeeping。

导出刻意排除 OAuth credential 的 keyed **fingerprint**、provider raw payload／**GPS** track 与 internal **owner id**。

删除产品数据无法删除三个产品边界之外的内容：已经写进 **Intervals.icu 日历** 的 workout、你在 **Intervals.icu Settings** 给出的 provider 授权、以及不含 plan／健康／identity 内容的最小化平台**营运紀錄／运营日志**。

完整生命周期见 [docs/account-lifecycle.md](docs/account-lifecycle.md)，公开隐私政策见 [paceandstaystrong.com/privacy.html](https://paceandstaystrong.com/privacy.html)。

---

## 技术文档

目前 release 对外有 **22 个 MCP tool**、**2 个 prompt**、**30 个 CLI 指令**、**3 份 JSON Schema contract**、**5 张 identity 表**。

- [用户故事](docs/user-story.md)
- [用户路径与对应的调用](docs/user-flows.md)
- [数据源边界](docs/data-sources.md)
- [客户端入口](entrypoints/README.md)
- [MCP protocol、OAuth 与 tool 行为](entrypoints/mcp/README.md)
- [Hosted gateway 部署](docs/deploy-gateway.md)
- [账号生命周期](docs/account-lifecycle.md)
- [发布／reviewer 材料](docs/distribution/README.md)
- [Release inventory](docs/release-inventory.md)
- [Repository invariants](AGENTS.md)

Long Run Hybrid Coach 是独立项目，与 Garmin、Intervals.icu、Apple 或其他设备／平台供应商没有隶属、背书或赞助关系。代码使用 [MIT License](LICENSE)。
