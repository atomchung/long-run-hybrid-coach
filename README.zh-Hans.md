# Long Run Hybrid Coach

[繁體中文](README.md) · [English](README.en.md) · **简体中文**

Long Run Hybrid Coach 是一个非官方、Intervals-first、device-agnostic 的个性化 hybrid training coach。它维护同一份 28 天方向与本周跑步＋力量训练计划，读取实际完成情况持续复盘，并可在你确认后把训练计划送到 Intervals.icu 日历。

Garmin 是目前第一条做过实机 dogfood 的下游设备路径，**不是使用前提**。Apple Watch、COROS、Polar、Suunto、Wahoo、其他 app／手表，甚至没有手表，都可以走同一个 Coach；差异只在有多少可信训练 evidence 能进入 Intervals.icu，以及 Intervals 后面能不能把训练计划同步到你的设备。

> **推荐普通用户直接使用 Hosted MCP：** `https://mcp.paceandstaystrong.com/mcp`。你需要一个 Intervals.icu 账号，但**不需要**自己创建 Intervals Developer App，也不需要自己架服务器。

---

## Quick Start：Hosted MCP（推荐）

### 使用前只需要准备三件事

1. 一个 **Intervals.icu 账号**。
2. 一个可以连接 remote MCP 的 ChatGPT、Claude 或其他 MCP client。
3. 可选：已经会把活动同步进 Intervals.icu 的手表或训练 app。

**不需要 Garmin。** Hosted 版本也**不需要自己创建 Intervals Developer App**。

### 1. 连接 Hosted Coach

MCP endpoint：

```text
https://mcp.paceandstaystrong.com/mcp
```

- **ChatGPT**：如果你的账号／workspace 当前支持自定义 MCP app／connector，创建一个自定义连接并填入上面的 endpoint；扫描或连接时完成 OAuth。ChatGPT 的自定义 MCP 能力仍会随套餐与 workspace rollout 不同，如果当前账号没有这个入口，可以先使用其他 MCP client，或等公开 listing 上架后直接从 Apps／directory 启用。
- **claude.ai / Claude Desktop**：Settings → Connectors → Add custom connector → 填入 endpoint。
- **其他 MCP client**：把同一个 URL 配置为 remote Streamable HTTP MCP server。

各入口目前是“已实机完整验证”还是“已封装、等待真实连接验证”，以 [entrypoints/](entrypoints/README.md) 的表为准。

### 2. 授权 Intervals.icu

连接后浏览器会打开 Intervals.icu 同意页面。登录**你自己的 Intervals.icu 账号**并授权 Coach 需要的权限：

- `ACTIVITY:READ`：读取已完成训练。
- `WELLNESS:READ`：读取 Intervals 可提供的 wellness evidence。
- `CALENDAR:WRITE`：读／写训练日历，让确认后的训练计划可以交付并 read-back 验证。
- `SETTINGS:WRITE`：读取设置；只有在已确认的 delivery 流程确实需要时，才会补上缺失且有 evidence 支持的跑步 threshold setting。

Intervals 同意页会把这些能力分开。少勾一项不代表整个 Coach 都不能用；依赖该权限的能力会明确失败。重新连接并补上权限即可，**不要把 Intervals 密码或 token 贴进对话**。

### 3. 直接问正常的教练问题

不需要先填问卷。例如：

```text
读我最近的训练，告诉我这周应该怎么练。
```

或：

```text
我想提升 VO2max，又不想掉力量，帮我排第一个 28 天方向。
```

Coach 会先读取已经存在的数据，再只询问真正会改变决策的缺口，例如本周可训练日期、器材，或 provider 不可能知道的力量训练 baseline。

### 4. 看完预览，确认一次才建立／修改计划

第一次建立计划时，会先显示完整 28 天 preview：

- **本周**：精确、可执行、可交付的 session。
- **后三周**：方向性 outlook，不假装现在就知道全部细节。

你确认这份 preview 后，计划才会写入。之后每周改动也是同样体验：

**before / after preview → 一次确认 → apply**。

### 5. 想让训练计划进入日历时，再做一次 delivery 确认

交付是另一个独立确认：

**delivery preview → 一次确认 → 写入 Intervals.icu → read-back 验证**。

本产品能证明的最远状态是 `intervals_accepted`。**Intervals 成功不等于训练计划已经在 Garmin、Apple Watch 或其他手表上。** Intervals 后面的同步是另一个外部 hop，需要按设备路径分别验证。

---

## Intervals.icu 在这个产品里做什么？

Intervals.icu 是目前的 **interoperability hub**：它帮助 Coach 接收不同设备／app 的活动和 wellness 数据，也承接 Coach 确认后的日历训练计划。它**不是 Coach 的 PlanState source of truth**。

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
- **你的手表／app**：可以把活动带进 Intervals，也可能接收 Intervals 往下发送的 workout；但最后一跳是否成功是独立 compatibility evidence。

### 使用前 Intervals 里一定要有什么？

只有**账号本身**是必要条件。有活动／wellness 已经同步进去，Coach 的自动 evidence 会更完整；没有的字段保持 unknown，不会被当成 0，也不会因为少一个可选指标就阻断正常 coaching。

设备量不到或没有同步的内容，可以直接在对话里告诉 Coach，例如：

- 力量训练实际组数、重量与次数；
- 本周可训练时间与器材限制；
- 体重／体脂；
- 没带表的一次活动；
- “最近很累”“睡不好”这类 subjective state；
- 你从手表／app 实际看到的 sleep、HRV、resting HR、readiness 等 recovery reading。

Coach 不会把一句“我很累”偷偷转换成一个假的 readiness score。

---

## Hosted MCP vs Local / Self-hosted MCP

| | Hosted MCP（推荐） | Local / Self-hosted MCP |
| --- | --- | --- |
| MCP URL | `https://mcp.paceandstaystrong.com/mcp` | 你自己的 gateway，例如 `http://127.0.0.1:8422/mcp` |
| 运维 | 不用自己维护 server | 自己启动、更新、备份和维护 |
| Intervals Developer App | **不需要** | **需要**自己的 OAuth app credential |
| current plan 存在哪里 | hosted per-athlete owner store | 你自己的 gateway state root |
| 适合谁 | 普通用户、多 client 共用同一计划 | 开发者、需要完全自管环境／数据的人 |
| ChatGPT | 账号／workspace 支持相应 MCP action 时可以直接连接 remote endpoint | ChatGPT 不能直接访问 localhost；需要受支持的 tunnel 或可访问 HTTPS endpoint |

### Hosted MCP 怎么启用？

最短流程：

1. 在 MCP client 新增 remote MCP app／connector。
2. URL 填 `https://mcp.paceandstaystrong.com/mcp`。
3. 选择／完成 OAuth。
4. 浏览器进入 Intervals.icu 授权。
5. 回到聊天后直接问第一个教练问题。

Hosted 服务会自己处理 dynamic client registration、PKCE、gateway token 与 per-athlete owner mapping；普通用户不需要 owner id、athlete id、API key、Intervals client secret 或任何 server environment variable。

### Local / Self-hosted MCP 怎么跑？

Repo 使用 Python 3.11，产品本身是 stdlib-only，不需要先 `pip install` 一串 runtime package。

1. Clone repo。
2. 在 Intervals.icu Settings／Developer 创建自己的 OAuth application。
3. 在 Intervals app 里注册 gateway provider callback：`<gateway-origin>/oauth/callback`。仅本机 client 使用时可以走 loopback；如果 remote client 要连接，请在 gateway 前放可访问的 HTTPS／secure tunnel，不要把开发机裸露到公网。
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

如果要把自己的 gateway 正式提供给远程 client，不要把上面的 loopback 示例当成 production runbook。Persistent volume、TLS、trusted client origin、single replica、release identity 和部署验证都在 [docs/deploy-gateway.md](docs/deploy-gateway.md)。

### Local CLI 与 Hosted 不应该悄悄变成两份 current plan

一个 athlete 应该只有一个 current writer。当本机已经设置 `GARMIN_COACH_LOOP_GATEWAY_URL` 指向 hosted coach 时，本机 store 写入默认会被阻止；只有明确加 `--offline` 才代表“我刻意在做另一份 local plan”。

已有本机 state 可以迁移到 hosted，迁移后把 local store seal 起来，完整流程见 [docs/ops/migrate-local-store-to-hosted.md](docs/ops/migrate-local-store-to-hosted.md)。

---

## 现在可以做什么？

- 维护一份 **28 天方向**：本周精确 session ＋ 后三周 outlook。
- 读取 Intervals activity／wellness／calendar evidence，并把可信的 planned → actual 自动 reconciliation 回 current plan。
- 在同一份周计划中同时处理跑步与力量训练。
- 记录 athlete-reported profile、availability、long-term goal、training preference、实际力量训练、体重／体脂、设备没记录到的活动，以及 subjective state。
- `startCoachSession` 接收本次 request 的 recovery readings；Hosted 不需要也不会读取你的本机 health database。
- 导入支持的历史 evidence，包括支持格式的 CSV、Apple Health XML 内容，以及通过 binary import path 处理的 FIT payload；同文件与同活动会做 deterministic 去重，无法判断时才询问用户。
- Session 可以带 `coach_note`，让教练重点文字一起进入 Intervals event，但不会把自然语言偷偷变成另一套 workout grammar。
- 每周复盘“实际练了什么、有没有进步证据、下一步是什么”，而不是把“完成计划”直接当成 fitness 已提升。
- 计划改动先 preview，再一次确认后 apply。
- 日历交付先 preview，再一次确认；支持安全 retry、replace 与 withdraw product-owned event。
- 在对话里直接导出本产品持有的 owner data。
- 在对话里通过 preview → 一次确认 → receipt 永久删除本产品持有的 owner data。

### 重要边界

- `startCoachSession` 会做 deterministic reconciliation，**可能写入新的 PlanState version**；如果只需要完全无 side effect 的存储状态，使用 `getCoachState`。
- athlete-reported activity 是 evidence，但不会被偷偷升级成 provider-backed actual completion。
- recovery 数字只接受实际观察值；模型不能从文字自己猜一个数字。
- 本产品不做医疗诊断。
- Delivery 证据只到 Intervals read-back，不会声称已经到手表。

---

## 数据、导出与删除

Hosted 保存的是维持同一 owner 计划所需要的产品状态：PlanState version chain、decision／receipt、athlete-reported evidence、identity mapping，以及未收敛 delivery bookkeeping。

导出会刻意排除 OAuth credential fingerprint、provider raw payload／GPS track 与 internal owner id；这些分别属于单向安全 bookkeeping、provider 原始数据与内部 storage locator。

删除产品数据也有三个明确边界：已经写进 Intervals.icu 日历的 workout、Intervals.icu Settings 中的 provider 授权、以及不含计划／健康／identity 内容的最小化运营日志，都不在本产品可删除范围。

完整生命周期见 [docs/account-lifecycle.md](docs/account-lifecycle.md)，公开隐私政策在 [paceandstaystrong.com/privacy.html](https://paceandstaystrong.com/privacy.html)。

---

## 当前限制

- Coach 不直接登录 Apple Health、Garmin Connect 或其他设备账号；主要自动 evidence 路径目前仍是 Intervals.icu。
- Hosted 不会永久保存每次 request 上传的 raw recovery data；下次需要时重新提供当前 evidence。
- 本产品无法观察 Intervals 后面的每一个设备同步 hop，所以不会把 `intervals_accepted` 说成“已经在手表上”。
- Local self-hosting 是 operator／developer 路径；普通用户应优先 Hosted MCP。
- 设备兼容性按路径验证，不会因为 Garmin 已验证就推断 Apple Watch 或其他设备一定相同。

---

## 技术文档

- 用户故事：[docs/user-story.md](docs/user-story.md)
- 数据源边界：[docs/data-sources.md](docs/data-sources.md)
- 客户端入口：[entrypoints/](entrypoints/README.md)
- MCP protocol / OAuth / tool 行为：[entrypoints/mcp/README.md](entrypoints/mcp/README.md)
- Hosted 部署：[docs/deploy-gateway.md](docs/deploy-gateway.md)
- 账号生命周期：[docs/account-lifecycle.md](docs/account-lifecycle.md)
- 发布／reviewer 材料：[docs/distribution/](docs/distribution/README.md)
- Release inventory：[docs/release-inventory.md](docs/release-inventory.md)
- Repository invariants：[AGENTS.md](AGENTS.md)

Long Run Hybrid Coach 是独立项目，与 Garmin、Intervals.icu、Apple 或其他设备／平台供应商没有隶属、背书或赞助关系。代码使用 [MIT License](LICENSE)。
