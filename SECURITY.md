# Security Policy

## Reporting a vulnerability

**Two channels, either is fine:**

1. **GitHub private advisory** — Security tab → Advisories → Report a
   vulnerability. Preferred, because it keeps the report attached to the fix.
2. **Email** — **asas7412589630@gmail.com**

Please do **not** open a public issue for anything that could let a stranger
reach someone's proxy or read their prompts.

Include: the version (or commit), the platform, and the smallest reproduction you
can manage. A `curl` line is ideal.

Expect an acknowledgement within a few days. This is a single-maintainer project,
so a fix may take longer than that — but the report will not be ignored.

---

## Threat model

The proxy is expected to run on a device the operator controls — a phone, a
laptop, a small VPS — and to hold two kinds of secret:

| Secret | Where it lives | Consequence if leaked |
|---|---|---|
| **Upstream API key** | `proxy/upstream_keys.txt`, `proxy/source_secrets.json` | someone spends your quota |
| **Client key** (`triad-…`) | `proxy/keys.json` (SHA-256 only) | someone uses your proxy, and through it your quota |

Neither is ever written to a response body, a log line, or a webpage.

### What an attacker on the same network can do

Nothing, by default. The listener binds `127.0.0.1`. Reaching it requires either
an SSH tunnel or an explicit `ARMOR_BIND=0.0.0.0`, and the latter still requires a
client key.

### What a web page on the same device can do

This is the case the design spends the most effort on, because it is the one most
people assume is safe. It is not: a page in the phone's own browser reaches
`127.0.0.1` exactly as curl does.

Defences, in order:

1. **CORS allow-list derived from server-side facts.** `Host`, `Origin` and
   `X-Forwarded-Host` are caller-written and are not consulted. The allow-list is
   the bind address, a tunnel URL that `tunnel_probe` verified by DNS and by
   fetching `/healthz`, and any explicit `ARMOR_CORS_ORIGIN`.
2. **`Origin: null` refused by default.** It is sent by `file://` pages,
   sandboxed iframes and `data:` URLs; only the first is a console, and that
   console is also served over http so it needs no exemption.
3. **Loopback + browser provenance requires a key.** A browser stamps `Origin`
   and `Sec-Fetch-*` on every request and a page cannot omit or forge them, so
   "has browser provenance" reliably means "driven by a page". Local tools send
   neither and are still allowed through.
4. **Admin writes require a real credential** — a valid client key, or a 32-byte
   random token in `proxy/setup_token.txt` readable only through a same-origin
   bootstrap endpoint that emits no CORS header.

CORS alone is not enough: it stops a page *reading* a response, not *sending* a
request. A `Content-Type: text/plain` POST is a simple request and needs no
preflight, and a write does not have to be read to be harmful. That is why rule 3
exists.

### What a stranger with the URL can do (tunnel mode)

| Endpoint | Requires |
|---|---|
| `/healthz` | nothing — returns `{"ok":true}` only |
| `/setup/*` | setup token (dies when the first client key is created) |
| `/keys`, `/sources`, `/panel/*`, `/v1/*` | client key |
| everything else | 404 `proxy.no_route` |

A fresh install with no key is **not** an open relay: only the shell, liveness and
the setup calls are reachable, and the data endpoints need the setup token, which
only the device can read.

### What this does not defend against

Stated plainly, because pretending otherwise is worse than the gap:

- **A compromised host.** If someone has root, they have the keys.
- **A malicious client you handed a key to.** A key is a capability; share
  sparingly and revoke from the console.
- **Traffic analysis.** TLS protects content, not metadata.
- **Prompt extraction by the upstream provider.** The armor is sent to them in
  plaintext inside the request. Whatever their retention policy is, it applies.
- **Egress IP reputation.** If a shared host's address is on a WAF list, requests
  are blocked before reaching the model. Header normalisation helps against
  header-sniffing WAFs and does nothing against IP-reputation ones.

---

## Hardening checklist for a public deployment

```sh
# 1. ephemeral: nothing on disk
export ARMOR_EPHEMERAL=1

# 2. loopback only; reach it over SSH
export ARMOR_BIND=127.0.0.1

# 3. create a client key BEFORE opening any tunnel
#    (console → 金鑰 → 建立)

# 4. confirm the old bootstrap hole is closed
curl -X POST localhost:8412/setup/apply \
  -H 'X-Armor-Admin: 1' -H 'Content-Type: application/json' -d '{}'
# expect 401

# 5. confirm CORS does not reflect a forged origin
curl -sD- -o /dev/null localhost:8412/keys \
  -H 'Host: evil.example' -H 'Origin: http://evil.example' | grep -i access-control
# expect no output
```

---

## Supported versions

The `main` branch is the only supported version. Fixes land there and nowhere
else; there are no maintained back-branches.

---

Made by yuangeluyou.com :)
