# Deployment

Four layouts, from "just my laptop" to "a small VPS serving a few people". Pick the
one that matches, not the biggest one.

| Layout | Exposure | State on disk | For |
|---|---|---|---|
| [Local](#1-local) | loopback only | full | development, one machine |
| [SSH tunnel](#2-ssh-tunnel-recommended-for-a-vps) | loopback only | full or none | a VPS you reach yourself |
| [Ephemeral](#3-ephemeral-shared-host) | tunnel or LAN | **none** | a shared host, or any host you do not fully trust |
| [LAN](#4-lan) | `0.0.0.0` | full | phone, tablet, another PC at home |

---

## 1 · Local

```sh
unzip cinder-server-v2.zip -d cinder-server
cd cinder-server
chmod +x triad-up triad-deploy triad-tunnel run-proxy.sh run-tunnel.sh
./triad-up
```

Console at `http://127.0.0.1:8412/app/`.

Pass `--restart` after editing `blocks/` — the armour is cached for 3 seconds.

---

## 2 · SSH tunnel (recommended for a VPS)

The safest remote setup: the server opens **no port at all**. Your machine
connects to the server's loopback through SSH.

**On the server**

```sh
sudo apt install -y python3 unzip
unzip cinder-server-v2.zip -d ~/cinder-server
cd ~/cinder-server && chmod +x triad-up
./triad-up                        # binds 127.0.0.1:8412
```

**On your machine**

```sh
ssh -N -L 8412:127.0.0.1:8412 user@server
```

Now `http://127.0.0.1:8412/v1` on your machine is the server's proxy.

Why this beats a tunnel with a public URL: nothing to scan, nothing to
brute-force, no WAF to appease, and no third party in the path. It costs one
`ssh` process.

Keep it up:

```sh
# ~/.ssh/config
Host cinder
    HostName server.example.com
    User ubuntu
    LocalForward 8412 127.0.0.1:8412
    ServerAliveInterval 30
```

then `ssh -N cinder`.

---

## 3 · Ephemeral (shared host)

For a host you do not fully control, or one where you do not want a transcript
sitting on disk.

```sh
./triad-deploy
```

That is the whole instruction. It sets `ARMOR_EPHEMERAL=1`, binds loopback, and
prints how to reach it.

### What "ephemeral" actually means

Nothing is written to disk. Not less — nothing.

| Would have been written | Now |
|---|---|
| `proxy/logs/requests.jsonl` | not created |
| `logs/<client>/<conversation>.log` | not created |
| `runtime/trace/` per-turn records | not created |
| `last_usage.json` token counts | memory only |
| `proxy/accounts.json` password hashes | layer not loaded |
| `fp_debug.jsonl` (first 220 chars of prompt) | not created |
| `last_request.json`, `last_body.json` | not created |
| `proxy/setup_token.txt` | memory only, rotates each restart |

Upstream keys are still **read** — they are inputs, not records.

### Verifying it yourself

```sh
ARMOR_EPHEMERAL=1 ./triad-up
curl -s -X POST http://127.0.0.1:8412/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"glm-5.3","messages":[{"role":"user","content":"UNIQUE-MARKER-42"}]}'

grep -r UNIQUE-MARKER-42 .          # expect: no output
find logs state runtime artifacts -type f | wc -l    # expect: 0
```

### The cost

**Restarting loses every client key you created.** The key records live in memory.
Copy your key out before restarting, or run with `--persist` if that trade is
wrong for you.

---

## 4 · LAN

```sh
ARMOR_BIND=0.0.0.0 ./triad-up
```

Find your address:

```sh
ip addr | grep 'inet 192'          # Linux
ipconfig getifaddr en0              # macOS
ipconfig                            # Windows
```

Clients then use `http://192.168.1.100:8412/v1`.

**Two warnings, both real:**

1. **Create a client key first.** With the key store empty, only the setup
   endpoints are reachable — but you will not get a warning until you try.
   `./triad-deploy` refuses to bind publicly without an upstream key for exactly
   this reason.
2. `0.0.0.0` means every device on the network can reach the port. On hotel,
   campus or café wifi, that is not your network. Use the SSH tunnel instead.

---

## systemd

```sh
./triad-deploy --systemd > /tmp/cinder.service
sudo mv /tmp/cinder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cinder
```

Ready-made units with hardening directives are in
[`examples/systemd/`](../examples/systemd/).

Two operational notes:

- `TimeoutStopSec=15` — the proxy drains in-flight requests on `SIGTERM`
  (default grace 6 s). A shorter timeout cuts streams mid-chunk.
- In ephemeral mode the only record is journald. That is deliberate: journald's
  retention and access control beat a log file sitting next to the keys.

---

## Public URL

Only if clients cannot use SSH.

```sh
./triad-tunnel        # prints https://xxxx.trycloudflare.com
```

The URL **changes on every restart**. `triad-tunnel` validates the new hostname
(DNS + `/healthz`) before writing it down, because serving a cached hostname is
what makes a client report "service unavailable" against a healthy proxy.

For a stable hostname you need a domain:

```sh
cloudflared tunnel login
cloudflared tunnel create cinder
cloudflared tunnel route dns cinder api.example.com
cloudflared tunnel run --url http://127.0.0.1:8412 cinder
```

Add a Cloudflare Access policy so the URL being public does not mean the proxy is.

### Before you open any tunnel

```sh
# 1. a client key must exist
curl -s localhost:8412/healthz | grep -o '"client_keys": [0-9]*'

# 2. the old bootstrap hole must be closed
curl -X POST localhost:8412/setup/apply -H 'X-Armor-Admin: 1' \
  -H 'Content-Type: application/json' -d '{}'
# expect 401
```

If `client_keys` is `0`, stop and create one in the console first.

---

## Free hosting

Short version, from actually reading the providers' pricing pages:

| Platform | Free tier | Always on | Asia |
|---|---|---|---|
| **Oracle Cloud Always Free** | 4 OCPU / 24 GB ARM, permanent | yes | Tokyo, Seoul, Singapore |
| Fly.io | trial credit, then metered | yes | yes |
| Render | 750 h/month, **sleeps after 15 min idle** | no | no |
| Cloud Run | 2 M requests/month, scale-to-zero | yes | yes |

Only Oracle is both free indefinitely and always on. It wants a card for
verification (no charge) and ARM instances are often out of capacity.

Full walkthrough, including the idle-reclamation workaround:
**[HOSTING.md](HOSTING.md)**

---

## When the host's IP is not trusted

Some providers sit behind a WAF that blocks datacentre and shared-host addresses.
The symptom is a `403` whose body is HTML, not JSON:

```
<title>Attention Required! | Cloudflare</title>
```

The proxy now reports this as `upstream.edge_blocked` instead of
`upstream.auth` — the key is fine, the *edge* refused, and the request never
reached a model.

What is already done for you: library User-Agents (`python-urllib`, `curl`,
`wget`, `java`, `go-http-client`) are rewritten, and `Accept-Language` / `Accept`
are filled in.

```sh
ARMOR_UPSTREAM_UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) \
AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15" ./triad-up
```

**And what is not:** if the address itself is blacklisted, headers will not save
it. WAFs look at IP reputation, TLS fingerprint and behaviour. Order of fixes:

1. Use the provider's official domain rather than a relay.
2. Move the host's region.
3. Residential egress (costs money).
4. Host the model yourself.

---

## Upgrading

```sh
./triad-up --status
kill "$(cat triad.pid)"

unzip -o cinder-server-v2.zip -d .       # -o overwrites code, keeps proxy/

./triad-up
```

`proxy/` holds your keys and settings and is never inside the archive's changed
files, so an overwrite is safe. `blocks/` **is** replaced — back it up first if you
edited it.

---

Made by yuangeluyou.com :)
