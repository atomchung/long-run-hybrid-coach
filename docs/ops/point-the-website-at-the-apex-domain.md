# Pointing the website at paceandstaystrong.com

The public website lives in its own repository and is served by GitHub Pages at the
project URL. The canonical address is the apex `paceandstaystrong.com`, whose DNS is at
Cloudflare (registrar and nameservers both). This file is the sequence that moves it
there.

All of it is console work in two places -- GitHub and the Cloudflare dashboard. None of
it is scriptable from this repository, and none of it needs a Cloudflare API token.

## This was carried out on 2026-08-18

The site is live on the apex. What follows is kept as the procedure -- for a rebuild, a
second domain, or a reader asking why a step exists -- and this section says which parts
of it actually ran, because a runbook that reads as pending after it has been done sends
somebody to redo it.

Done, in this order: the four `A` records, then the four `AAAA` records, then a `www`
`CNAME` to the pages host, every one of them **DNS only**; then the **Custom domain**
field on the website repository; then **Enforce HTTPS** once the certificate came back
`approved`. Steps 2 through 6 below, in other words.

Evidence, read back afterwards rather than off the panels: all four A and all four AAAA
records answering at Cloudflare and at an outside resolver; `/`, `/privacy.html`,
`/terms.html` and `/support.html` each `200` over HTTPS on the apex with a certificate
that validates; `gh api` reporting `cname` at the apex, the certificate `approved` and
`https_enforced` true; the old project URL now `301`ing to the apex; and
`mcp.paceandstaystrong.com` unchanged with `/readyz` still `200`.

**Two things did not happen, and neither blocks anything today.**

- **Step 1, claiming the domain in GitHub, was skipped.** The apex is now attached to the
  website repository, so it cannot be claimed out from under it while that stays true --
  but the verified-domains entry is what keeps that from being reversible by someone else
  if it ever detaches. Worth closing; not urgent.
- **The certificate covers the apex only, not `www`.** GitHub usually extends it once the
  `www` record has been in place a while. Until it does, `https://www.paceandstaystrong.com`
  fails to handshake, so publish the apex form of every URL and re-check with the loop in
  step 6.

One local-machine trap, since it cost time here: macOS caches the *absence* of a record
too, so a Mac that looked the domain up before the cutover keeps failing after it while
`dig` and the rest of the world succeed. `sudo dscacheutil -flushcache; sudo killall -HUP
mDNSResponder` clears it. Verify from something that is not this machine before believing
a failure.

## Where this started (verified 2026-08-18, before the cutover)

Nothing is half-done, and nothing here is a formality -- the zone is empty at the apex.
Confirm it before starting, straight at Cloudflare's own nameservers so a cached answer
cannot flatter the result:

```bash
for type in A AAAA CNAME TXT; do
  dig @nitin.ns.cloudflare.com +noall +comments paceandstaystrong.com "$type" | grep status:
done
dig @nitin.ns.cloudflare.com +noall +comments www.paceandstaystrong.com A | grep status:
dig @nitin.ns.cloudflare.com +short mcp.paceandstaystrong.com CNAME
```

On 2026-08-18 the apex answered `NOERROR` with no records of any type, `www` answered
`NXDOMAIN`, and `mcp` resolved to its Railway target. An earlier stale apex `CNAME`
pointing at a Railway host had already been removed, so the zone has been touched
without anything being put in its place. The website therefore answers only on the
GitHub project URL, and `cname` on the Pages side is still null:

```bash
gh api repos/:owner/:repo/pages --jq '{cname, https_enforced, protected_domain_state}'
```

Every step below adds a row; none of them edits an existing one.

## Two facts that set the order

**The `CNAME` file in the website repository is not the mechanism.** That site publishes
through a GitHub Actions workflow, and Pages ignores a `CNAME` file on that publishing
path -- it is the committed record of the decision, and it becomes load-bearing only if
the publishing source ever moves back to a branch. What actually attaches the domain is
the **Custom domain** field in the website repository's Pages settings.

**Setting that field moves the site immediately.** From the moment it is saved, Pages
redirects the old project URL to the apex. If DNS is not answering yet, the website is
dark until it is. So the DNS records go in first and that field is set near the end --
with step 1 closing the takeover window that ordering would otherwise open.

## 1. Claim the domain in GitHub before pointing anything at it

Account **Settings → Pages → Add a domain**, enter `paceandstaystrong.com`. GitHub prints
one TXT record. Add it at Cloudflare (**DNS → Records → Add record**):

| Type | Name | Content | Proxy | TTL |
| --- | --- | --- | --- | --- |
| TXT | `_github-pages-challenge-<github-username>` | the code GitHub just printed | n/a for TXT | Auto |

Then press **Verify** in GitHub. This is why it comes first: an apex pointed at GitHub's
IPs that no account has claimed can be claimed by a different account, which is a
takeover of the domain's website. Verification does not touch the live site.

## 2. Add the apex records at Cloudflare, proxy off

**DNS → Records → Add record**, one row per address. `@` is the apex.

| Type | Name | Content | Proxy | TTL |
| --- | --- | --- | --- | --- |
| A | `@` | `185.199.108.153` | DNS only (grey cloud) | Auto |
| A | `@` | `185.199.109.153` | DNS only (grey cloud) | Auto |
| A | `@` | `185.199.110.153` | DNS only (grey cloud) | Auto |
| A | `@` | `185.199.111.153` | DNS only (grey cloud) | Auto |
| AAAA | `@` | `2606:50c0:8000::153` | DNS only (grey cloud) | Auto |
| AAAA | `@` | `2606:50c0:8001::153` | DNS only (grey cloud) | Auto |
| AAAA | `@` | `2606:50c0:8002::153` | DNS only (grey cloud) | Auto |
| AAAA | `@` | `2606:50c0:8003::153` | DNS only (grey cloud) | Auto |

The four A records are what GitHub requires for an apex; the four AAAA records are the
IPv6 half of the same set and are optional only in the sense that IPv6 visitors are.

Optional, and only if the `www` name should also work: one CNAME, `www` →
`<github-username>.github.io`, also DNS only. Pages redirects between the two once both
are configured.

**Proxy off is not a preference.** GitHub issues the certificate by answering a challenge
on those IPs; behind Cloudflare's proxy the challenge never reaches GitHub, so HTTPS never
provisions.

**Do not touch the `mcp` record.** `mcp.paceandstaystrong.com` is a CNAME to the
gateway's Railway target, and it is the address every connected client holds. Nothing in
this procedure changes it; a change to it signs everyone out.

## 3. Wait until the records answer

```bash
dig @nitin.ns.cloudflare.com +short paceandstaystrong.com A
dig @nitin.ns.cloudflare.com +short paceandstaystrong.com AAAA
dig +short paceandstaystrong.com A
```

The first two ask Cloudflare directly and answer "did the record save"; the third asks
whatever resolver this machine uses and answers "has it reached the world". Expect exactly
the addresses above and nothing else. Cloudflare usually publishes within a minute; GitHub
documents up to 24 hours for propagation generally.

## 4. Set the custom domain on the website repository

Website repository → **Settings → Pages → Custom domain** → `paceandstaystrong.com` →
**Save**, and wait for "DNS check successful". This is the step that moves the site.

From the website checkout, read the result back from the API rather than the panel:

```bash
gh api repos/:owner/:repo/pages --jq '{cname, https_enforced, protected_domain_state}'
```

`cname` is now the apex, `protected_domain_state` reflects step 1, and `https_enforced` is
still false until the next step.

## 5. Enforce HTTPS

The certificate is usually issued within minutes; GitHub documents up to 24 hours before
the option becomes available. When **Enforce HTTPS** is no longer greyed out, tick it.

If the apex is ever put behind Cloudflare's proxy later, two Cloudflare settings decide
whether the site still loads:

- **SSL/TLS → Overview → encryption mode must be Full** (or Full (strict)). On
  **Flexible**, Cloudflare fetches from GitHub over plain HTTP, GitHub's HTTPS enforcement
  redirects that request to HTTPS, and Cloudflare fetches over HTTP again -- an infinite
  redirect loop, seen by visitors as `ERR_TOO_MANY_REDIRECTS`.
- **Always Use HTTPS** is then harmless and redundant with GitHub's own enforcement. It is
  only the second half of that loop when the mode is Flexible.

Leaving the apex DNS-only avoids both settings entirely, which is the recommendation here.

## 6. Read the site back on the real domain

The Pages panel reporting success is the plan; these four responses are the account.

```bash
for page in "" privacy.html terms.html support.html; do
  curl -s -o /dev/null -w "%{http_code} %{url_effective}\n" -L "https://paceandstaystrong.com/$page"
done

curl -s -o /dev/null -w "%{http_code} -> %{redirect_url}\n" http://paceandstaystrong.com/
```

Four `200`s over HTTPS, and plain HTTP redirecting to HTTPS. Then confirm the gateway is
where it was:

```bash
curl -s https://mcp.paceandstaystrong.com/readyz | python3 -m json.tool
```

## 7. Move the documented links over

Once the apex answers, the project URL is a redirect rather than an address. Swap the
links that name it -- `README.md` here, plus anything under `docs/` -- to
`https://paceandstaystrong.com/...`. Leave `PUBLIC_SELF_REFERENCES` in
`scripts/check_repo_safety.py` alone: the repository links beside them still need it.

## Still blank: the support address

`support.html` publishes the issue tracker as the way to reach a person, and says in so
many words that a direct address is not published yet. That matches the standing decision
-- the issue tracker is the entry channel for now, and a mailbox that cannot expose health
data comes before public testing -- but a platform submission generally wants a mailbox
rather than an issue tracker, so this is the one field on the website a submission can be
blocked on. Choosing it is the owner's call; when it exists it replaces the marked line in
`support.html` and the same sentence in section 10 of `privacy.html`, and nothing else on
the site changes.
