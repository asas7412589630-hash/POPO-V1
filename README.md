# Cinder Server

**A local OpenAI-compatible proxy that injects a system prompt you control, retries refusals, and keeps nothing on disk.**

Runs on Linux, Android/Termux, macOS and Windows. Pure Python standard library — no
`pip install`, no build step, no daemon dependencies.

```
your client  ──►  this proxy  ──►  your upstream provider
 (any OpenAI       (injects your      (OpenAI-compatible:
  client)           system prompt,     OpenAI, OpenRouter,
                    retries refusals,  Groq, vLLM, Ollama,
                    redacts state)     any /v1 endpoint)
```

---

## Why this exists

Most clients let you set a system prompt. Almost none let you do the four things a
working setup actually needs:

| Problem | Without a proxy | With Cinder Server |
|---|---|---|
| Long system prompt | paste 34k chars every session | injected per request, per model family |
| The model refuses once and the thread is dead | manual restart | up to 8 retries, each reframed |
| Provider blocks browser CORS | web clients unusable | proxy is same-origin to your client |
| "What prompt did it actually get?" | unknowable | `/armor?model=…` returns the exact text |
| Context window fills up | manual trimming | automatic compression + folding |
| Shared host leaks prompts into logs | log files everywhere | `ARMOR_EPHEMERAL=1` → zero writes |

---

## Quick start

```sh
unzip cinder-server-v2.zip -d cinder-server
cd cinder-server
chmod +x triad-up triad-deploy triad-tunnel run-proxy.sh run-tunnel.sh
./triad-up
```

Open **http://127.0.0.1:8412/app/** → the setup wizard walks four steps:
upstream URL + key → pick models and armor family → internal or external →
apply. It hands you a Base URL and an API key.

Then point any client at it:

```
Base URL : http://127.0.0.1:8412/v1
API Key  : triad-...
Model    : glm-5.3        (whatever you published)
```

Requires **Python 3.9+**. Nothing else.

---

## Running it as a service

```sh
./triad-deploy                       # ephemeral + loopback (recommended)
./triad-deploy --port 9000
./triad-deploy --systemd > /etc/systemd/system/cinder.service
```

`triad-deploy` refuses to bind a public address when no upstream key is set —
that combination is an open relay.

Reach it from another machine without exposing a port:

```sh
ssh -N -L 8412:127.0.0.1:8412 user@host
```

Full deployment guide: **[docs/HOSTING.md](docs/HOSTING.md)**

---

## What the proxy does to a request

```
POST /v1/chat/completions  {"model":"glm-5.3","messages":[…]}
  │
  ├─ 1. auth           client key (or setup token during bootstrap)
  ├─ 2. route          namespaced alias → its own source, else shared provider
  ├─ 3. armor          registry.json → glm family → persona + contract (34k chars)
  ├─ 4. shape          rewrite the user turn (config / demo / reasoning prefix)
  ├─ 5. normalise      reasoning effort, max_tokens, dropped fields per model
  ├─ 6. send           upstream /v1/chat/completions
  ├─ 7. inspect        refusal? downgrade? thin answer?
  │      └─ retry up to 8×, rotating keys, reframing each attempt
  └─ 8. deliver        plus X-Armor-* headers saying what happened
```

Every response carries diagnostics:

```
X-Armor-Request-Model   glm
X-Armor-Upstream-Model  glm-5.3
X-Armor-Policy-Len      34059
X-Armor-Retries         2
X-Armor-Verdict         retry_ok
```

Ask what a model would actually receive, without sending anything:

```sh
curl 'http://127.0.0.1:8412/armor?model=glm-5.3&level=native'
```

---

## Armor

The system prompt is built from named blocks in `blocks/`, selected per model
family by `blocks/registry.json`:

| Family | Blocks | Size |
|---|---|---|
| deepseek | `10_persona` + `00_contract` | 34,059 chars |
| glm | `10_persona` + `00_contract` | 34,059 chars |
| gpt | `00_contract` | 2,441 chars |

Edit the files, then `./triad-up --restart`. No rebuild, no packaging.

**Bring your own.** Nothing here is hard-coded to a particular prompt — replace the
files in `blocks/` with your own and update `registry.json`. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the block format.

---

## Refusal retry

A single refusal in the history poisons every later turn in that conversation.
Measured: the same question answers at 1,399 chars fresh, and at 173 chars of
refusal after one line of "做不到。". So refusals are removed from history before
each request.

Removing *plan* turns ("I'll start by checking the ELF header. Continue?") is a
separate switch and is **off by default** — measured against a live upstream,
keeping them produced answers at least as good, because a plan turn is context,
not noise.

| Switch | Default | Effect |
|---|---|---|
| `ARMOR_SCRUB_REFUSALS` | **on** | drop refusal turns from history |
| `ARMOR_SCRUB_PROSE` | off | drop narration-only turns |
| `ARMOR_RETRY_MAX` | 8 | retry ceiling per turn |

---

## Ephemeral mode

```sh
ARMOR_EPHEMERAL=1 ./triad-up
```

Nothing is written to disk. Verified by sending requests containing a unique
marker string and grepping the whole tree — **0 hits, 0 files created**.

What it disables:

| Layer | Disabled |
|---|---|
| modules | accounts (password hashes), per-turn trace, per-client log |
| switches | archive, state ledger, artifacts, compression, folding, body capture |
| writers | request log, usage ledger, fingerprint debug, all dump files |
| setup token | in-memory only, rotates every restart |

Upstream keys are still **read** — they are inputs, not records.

---

## Configuration

Everything is an environment variable; nothing is required.

| Variable | Default | Purpose |
|---|---|---|
| `TRIAD_PORT` | `8412` | listen port |
| `ARMOR_BIND` | `127.0.0.1` | bind address (`0.0.0.0` = public) |
| `ARMOR_EPHEMERAL` | `0` | `1` = write nothing to disk |
| `ARMOR_UPSTREAM_UA` | — | override the outbound User-Agent |
| `ARMOR_SOCKET_TIMEOUT` | `60` | per-connection timeout |
| `ARMOR_RETRY_MAX` | `8` | refusal retry ceiling |
| `ARMOR_MAX_CONCURRENT` | `12` | simultaneous requests |
| `ARMOR_CORS_ORIGIN` | — | extra allowed origins, comma-separated |

Full list: [docs/MANUAL.md](docs/MANUAL.md#附錄-a-環境變數)

---

## Security model

Three rules, each closing a hole that was measured rather than imagined:

1. **Trust is derived from server-side facts only.** The bind address, a
   DNS-verified tunnel URL, a random token on disk. Never from `Host`,
   `Origin`, or `X-Forwarded-Host`, which the caller writes.
2. **Loopback is not trust.** A web page on the same phone reaches `127.0.0.1`
   exactly like curl does. What separates them is that a browser stamps
   `Origin` / `Sec-Fetch-*` on every request and a local tool sends none.
3. **A constant is not a credential.** Admin writes require a valid client key or
   a 32-byte random setup token, not a header value that appears in the JS source.

See [SECURITY.md](SECURITY.md) for the threat model and how to report a problem.

---

## Tests

```sh
./run-tests.sh
```

28 suites. 26 run offline against a fake upstream; two need a real provider
(`test_drift.py`, and `test_concurrency.py` in live mode).

Note the two suites disagree about how to select offline mode — this is a wart,
not a design:

```sh
python3 test_clients.py --offline          # a flag
TRIAD_OFFLINE=1 python3 test_concurrency.py 4   # an env var; 4 = concurrency
```

---

## Repository layout

```
armor_proxy.py         the proxy (10.5k lines)
blocks/                system prompt blocks + registry
console/               web console (7 pages, no build step)
proxy/                 keys and settings (empty at rest)
policy/                model family / effort tables
docs/                  MANUAL, HOSTING, ARCHITECTURE
examples/              client snippets
test_*.py              28 test suites
triad-up               start / restart / status
triad-deploy           hosted + ephemeral setup
triad-tunnel           public URL via cloudflared
```

---

## Status

Working and in daily use. The refusal/armor pipeline is tuned against GLM,
DeepSeek and GPT families; other models work with the generic armour profile.

Known limits, stated plainly:

- `_forward_locked` is 2,149 lines in one function. It needs a golden test
  (recorded real upstream traffic) before it can be split safely.
- WAF-blocked egress IPs cannot be fixed from here. If a shared host's address is
  blacklisted, the change has to be the host or the provider.
- The console has no i18n; strings are Traditional Chinese.

---

Made by yuangeluyou.com :)
