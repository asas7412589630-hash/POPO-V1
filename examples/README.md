# Examples

Small, runnable snippets. Each one works against a proxy that is already running
(`./triad-up`) and has a client key created in the console.

Set the two values once:

```sh
export BASE_URL=http://127.0.0.1:8412/v1
export API_KEY=triad-...        # from the console: 金鑰 → 建立
```

| Directory | Shows |
|---|---|
| `curl/` | the raw HTTP shape, plus how to read the `X-Armor-*` diagnostics |
| `python/` | OpenAI SDK usage, and reading `/armor` to see what the model was told |
| `node/` | fetch with SSE streaming, no SDK |
| `systemd/` | service units for a hosted install |

## The one mistake everyone makes

`BASE_URL` is the **proxy**, not the provider. `API_KEY` is the key the **proxy**
issued, not your upstream key.

The upstream key stays on the proxy. A client never sees it, which is the point —
you can hand a client key to someone without handing them your provider account.

| | Value | Where it comes from |
|---|---|---|
| Base URL | `http://127.0.0.1:8412/v1` | the proxy (or its tunnel URL) |
| API key | `triad-…` | proxy console, 金鑰 page |
| Upstream key | `sk-…` | your provider — **never** put it in a client |

## Reading the diagnostics

Every response says what the proxy actually did:

```
X-Armor-Request-Model   what you asked for
X-Armor-Upstream-Model  what was sent
X-Armor-Policy-Len      chars of armour injected
X-Armor-Retries         attempts burned on refusals
X-Armor-Verdict         retry_ok · retry_failed · clean
```

If an answer looks wrong, these tell you whether the armour reached the model
(`Policy-Len` > 0), whether a refusal was retried (`Retries`), and whether the
retry worked (`Verdict`).

To read the armour itself:

```sh
curl -H "Authorization: Bearer $API_KEY" \
  'http://127.0.0.1:8412/armor?model=glm-5.3&level=native'
```

That returns the exact text the model would receive — not an approximation.
