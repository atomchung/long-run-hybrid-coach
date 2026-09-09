# Measure what a client displays, before renaming the server

`serverInfo` carries two names. `name` is the stable identifier — in Gemini Spark it is
also the `@` handle a user types. `title` is the human display string 2025-06-18 added
beside it. This gateway sends both (`SERVER_NAME` / `SERVER_TITLE`,
`garmin_coach_loop/mcp_transport.py`), and which one a client shows is the client's own
decision, not something the specification settles.

What is already observed (issue #380, 2026-09-09):

| client | displays | connection |
| --- | --- | --- |
| ChatGPT | Long Run Hybrid Coach | reads `title` |
| Claude | Long Run Hybrid Coach | reads `title` |
| Gemini Spark | Garmin Coach Loop | **an existing connection, re-synced** |
| OpenClaw | not observed | — |

The Spark row is not yet an answer. Re-syncing an existing connection refreshes the tool
list; it does not necessarily re-read a display name the client stored when the
connection was made. **A connection made fresh is the measurement**, and until one is
made, "Spark ignores `title`" is a guess.

## The measurement

Ten minutes, no deploy, nothing in production touched. It answers the question for any
client, not only Spark.

1. Serve a throwaway MCP endpoint that answers `initialize` with a `serverInfo` whose
   `name` and `title` are visibly different, and logs what the client sent:

   ```bash
   python3 - <<'PY'
   import json
   from http.server import BaseHTTPRequestHandler, HTTPServer

   class H(BaseHTTPRequestHandler):
       def do_POST(self):
           body = json.loads(self.rfile.read(int(self.headers["content-length"])))
           print("<-", json.dumps(body)[:400], flush=True)
           if "id" not in body:
               self.send_response(202); self.end_headers(); return
           result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                     "serverInfo": {"name": "name-field-abc", "title": "Title Field Xyz",
                                    "version": "0.0.1"}}
           if body.get("method") == "tools/list":
               result = {"tools": []}
           out = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode()
           self.send_response(200)
           self.send_header("content-type", "application/json")
           self.send_header("content-length", str(len(out)))
           self.end_headers(); self.wfile.write(out)
       def log_message(self, *a): pass

   HTTPServer(("127.0.0.1", 8787), H).serve_forever()
   PY
   ```

2. Publish it: `cloudflared tunnel --url http://127.0.0.1:8787`, and take the
   `https://<random>.trycloudflare.com` it prints.

3. Add `https://<random>.trycloudflare.com` as a **new** custom connector in the client
   under test, as the owner's own account. Read what the connector card, the
   authorization sheet and the `@` handle say.

4. Record which of `name-field-abc` and `Title Field Xyz` appeared where. The stdout log
   also records that client's `clientInfo`, which is worth keeping.

The endpoint has no OAuth and serves no tools, which is enough: the display name is
decided at `initialize`.

## If the client shows `name` and ignores `title`

Then, and only then, `SERVER_NAME` has to move for that client. What it costs:

- **Every connected client re-learns the name.** In Spark the `@` handle changes, so
  anyone already connected types the old one and reaches nothing until they notice.
  Existing tokens are unaffected — this is a display and addressing change, not an
  authorization one, so no reconnect is *required*, only a new handle to learn.
- **Four assertions** pin the current string: `tests/test_mcp_gateway.py:435`,
  `tests/test_hosted_entry.py:527`, `:554`, `:611`.
- **Not affected**, despite sharing the spelling: the local store path
  (`~/.local/share/garmin-coach-loop`), the CLI program name, the Skill directory and
  its frontmatter `name`, and the OpenClaw config key in
  [`entrypoints/openclaw/README.md`](../../entrypoints/openclaw/README.md) — that key is
  a local alias the user chooses at `openclaw mcp set`, not `serverInfo.name`. The MCP
  Registry entry (`server.json`) is already `long-run-hybrid-coach`.

`long-run-hybrid-coach` is the candidate that matches the repository, the website and
the registry. The counter-argument on record (issue #376) is that four words is a lot to
type after `@`; that trade-off only has to be made if this measurement says `title` is
being ignored.
