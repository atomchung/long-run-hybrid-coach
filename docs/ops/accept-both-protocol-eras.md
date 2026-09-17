# Accept both MCP protocol eras against one `/mcp`

One endpoint answers two protocol eras, and only a real client proves which one it picked.
This is how to find out, on a branch, before anything is deployed.

Use it whenever `python3 scripts/change_gates.py --base origin/main` names
`garmin_coach_loop/mcp_sdk_transport.py`, whenever the pinned `mcp` version in
`requirements.txt` moves, and once after any deployment that did either. It is not a
replacement for
[`accept-an-entry-after-a-surface-change.md`](accept-an-entry-after-a-surface-change.md):
that page is about what a model does with changed bytes, this one is about whether a
client connects at all.

## What the two eras look like on the wire

| | 2025 (`2024-11-05` … `2025-11-25`) | 2026-07-28 |
| --- | --- | --- |
| opens with | `initialize`, then `notifications/initialized` | `server/discover`, and nothing before it |
| protocol version | negotiated once, then stated in `MCP-Protocol-Version` | in `MCP-Protocol-Version` **and** in `params._meta` on every request |
| session | none here either way — this server is stateless | none by specification |
| routing headers | none | `Mcp-Method`, and `Mcp-Name` for `tools/call` / `prompts/get`, each checked against the body |

A client that opens with `server/discover` and is answered `400` falls back to 2025. That
fallback is invisible from the client's side and is exactly what this page exists to catch,
so the receipt below records the era of every request rather than only the outcome.

## Run it

1. **Start a gateway on loopback**, from the branch under test, with a state root of its
   own. Nothing here may point at the real store:

   ```bash
   export GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT="$(mktemp -d)/state"
   export GARMIN_COACH_LOOP_TOKEN_HMAC_KEY="$(openssl rand -base64 32)"
   python3 -m garmin_coach_loop.cli serve-gateway --host 127.0.0.1 --port 8422
   ```

   A disposable Intervals account is what makes step 3 a real answer rather than a
   provider error; connect it through the normal OAuth flow once.

2. **Point a 2026-07-28 client at it.** Claude Code speaks that era natively:

   ```bash
   claude --mcp-config '{"mcpServers":{"coach":{"type":"http","url":"http://127.0.0.1:8422/mcp"}}}' --strict-mcp-config
   ```

3. **Point a 2025 client at the same URL**, in the same process, without restarting it.
   Codex speaks 2025-06-18:

   ```bash
   codex --disable apps -c mcp_servers.coach.url="http://127.0.0.1:8422/mcp"
   ```

   `--disable apps` matters: without it Codex reaches the production connector instead,
   and the run proves nothing about this branch.

4. **Ask each one for a read and a write preview**, so the receipt covers `tools/list`,
   a `tools/call` that reaches Intervals, and a refusal.

## What makes it pass

Read the gateway's own log, not the client's summary:

- **Every request answered `200` or `202`.** A `400` carrying
  `unsupported_protocol_version` is the failure this page is looking for, and its count
  must be zero.
- **The 2026 client never sent `initialize`.** If it did, it fell back, and the run is a
  failure however well the conversation went.
- **The 2025 client's requests carried `2025-06-18`** (or no header on `initialize`) and
  were answered on the same endpoint, in the same process, without a restart.
- **The access line still names the tool and the outcome** — `tool=…​ outcome=…​` — on
  both eras. The tool call runs on the SDK's event loop rather than the request thread, so
  a missing name here means the request's provider-quota scope stopped travelling with it,
  and the operator log has gone quiet about what every MCP call did.
- **The same owner, both times.** `owner=` is one handle across both clients: two protocol
  eras are two ways in, never two accounts.

## Against production

After a deploy, the same two clients against the real domain, plus
[`verify-production-status.md`](verify-production-status.md) for `/readyz`. `/readyz`
additionally reports `mcp_sdk_version` — the SDK release the container actually resolved,
which is the deployment-side answer to the pin in `requirements.txt`. A rollout of this
kind asks nothing of a connected athlete: no reconnection, no OAuth, no extra
confirmation, and no protocol setting anywhere in any client. If a step here needs one,
that is a product change and the owner decides it (AGENTS.md, "A step the athlete has to
take is the owner's decision").
