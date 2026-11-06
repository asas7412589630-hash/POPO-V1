# Changelog

All notable changes. Format follows [Keep a Changelog](https://keepachangelog.com/);
versions follow [Semantic Versioning](https://semver.org/).

---

## [2.1.0] — 2026-11-07

### Added

- **Cross-platform CLI manager (`triad.py`)**: Native support for starting, restarting,
  stopping, and health checking the proxy across Windows, macOS, and Linux without bash.
- **PowerShell launcher (`triad-up.ps1`)**: Native launcher for Windows environments.
- **`AUTHORS.md`**: Formal co-creator and maintainer documentation (Minglink and asas7412589630-hash).

### Fixed

- **Global UTF-8 encoding**: Explicitly enforce `encoding="utf-8"` on all text `open()`
  calls across test suites and core tools, fixing `UnicodeDecodeError` on GBK/CP936 Windows.
- **Windows permission compatibility**: Fixed POSIX `0o600` permission assertion in
  `test_model_sources.py` on Windows NTFS.
- **Graceful shutdown on Windows**: Added `SIGBREAK` signal handler to `armor_proxy.py` and
  `CTRL_BREAK_EVENT` handling in `test_shutdown.py`, resolving abrupt process termination.
- **Test isolation**: Prevented test suites from writing transient credentials to the
  repository tree during execution.
- **Test matrix**: 25/25 offline test suites now pass cleanly on both Windows and Linux.

---

## [2.0.0] — 2026-11-06

First public release. The number is 2 rather than 1 because an earlier internal
build existed; nothing from that lineage is supported.

### Added

- **Ephemeral mode** (`ARMOR_EPHEMERAL=1`). Sends nothing to disk: no request
  log, no turn trace, no archive, no usage ledger, no accounts file, no setup
  token file. Verified by sending requests with a unique marker and grepping the
  tree — zero hits, zero files.
- **`triad-deploy`** — hosted setup that defaults to ephemeral + loopback, emits
  a systemd unit, and refuses a public bind when no upstream key is set.
- **`proxy.no_route`** error kind. An unknown path is now reported as "this proxy
  does not serve that route" instead of being forwarded and coming back as
  `upstream.model_not_found`, which sent operators to debug the model table.
- **`upstream.edge_blocked`** error kind. A CDN/WAF rejection (HTML body,
  `cf-mitigated`, `cf-error`) is no longer reported as `upstream.auth`.
- **Outbound header normalisation.** Library User-Agents (`python-urllib`, `curl`,
  `wget`, `java`, `go-http-client`) are rewritten and `Accept-Language` / `Accept`
  filled in, because a WAF tends to block those before the request reaches a model.
  `ARMOR_UPSTREAM_UA` overrides.
- **`/setup/token`** — same-origin bootstrap credential, 32 random bytes, refused
  once the first client key exists.
- **HTTP method coverage.** `PATCH`/`TRACE`/`CONNECT` and every error the base
  class raises now answer in the proxy's JSON envelope instead of an HTML page
  that advertises the Python version.

### Security

- **Admin writes require a real credential.** Replaces `X-Armor-Admin: 1`, a
  constant in the console's own JavaScript that authenticated nothing. One
  remote request could mint a permanent key, dump the transcript and overwrite
  the upstream credential.
- **CORS trust is derived from server-side facts.** The old check compared the
  request's `Origin` against its own `Host` header — both caller-controlled — so
  `Host: evil.example` + `Origin: http://evil.example` was answered as trusted.
- **Loopback no longer skips auth for browser-driven requests.** A page on the
  same device reaches `127.0.0.1` like curl does; `Origin` / `Sec-Fetch-*` reveal
  which is which. Closes the CSRF side-effect path CORS cannot cover.
- **A fresh install is no longer an open relay.** Previously every endpoint
  answered while the key store was empty, with one log line as the only warning.
- **`Handler.timeout = 60`.** 200 stalled connections used to pin 201 threads for
  at least four minutes; they now close within the minute.
- **`Origin: null` refused by default** (`ARMOR_CORS_ALLOW_NULL=1` to opt in).
- **401/403 key rotation is bounded to one pass** over the pool. It used to spend
  the entire 90 s budget on a single dead key.

### Fixed

- **`REQUIRE_CLIENT_KEY` was a local variable**, so switching 對外/對內 printed
  success, `healthz` agreed, and nothing changed until a restart.
- **`triad-up --restart` could leave the install dead.** A fixed 0.6 s sleep is
  shorter than the proxy's own drain, so the new process hit `Address in use`,
  the old one exited, and the pidfile pointed at a corpse.
- **Context scrubbing deleted the operator's own request.** `_is_empty_nudge()`
  treated any user message of ≤12 characters as a contentless prod, so
  `破解软件` (four characters) was removed along with the refusals it followed,
  and the model answered a leftover identity question. The classification is now
  a whitelist of actual continuation words, the last user message is protected
  unconditionally, and a scrub that would leave no user message is rolled back.
- **Narration removal defaults off.** Keeping a plan turn in history measured as
  good or better than deleting it (1062 chars/23 terms vs 978/10), because a plan
  turn is context. `ARMOR_SCRUB_PROSE=1` still enables the old behaviour.
- **`bench.py` resolved `../proxy/`** — a path from before the package moved into
  its own directory — and indexed `[0]` on a possibly empty key list. Every tool
  importing it (`rewriter`, `effort_tune`, `armory`, and through `structural`,
  the reframe layer that runs on every request) died at import, so reframe had
  been silently dead since packaging.
- **Unpublished namespaced aliases were routable.** `srcdemo/beta-9` for a model
  that was never published fell through to the shared provider and answered.
- **`test_sources_integration.py` depended on the live internet.** Without
  `ARMOR_UPSTREAM_HOST` an unpublished alias reached the real provider, which
  takes ~25 s to 404 an unknown model — longer than the client timeout, so the
  suite failed in a way that looked like a proxy bug. Now hermetic.

### Project

- **Licence: CC BY-NC 4.0.** Commercial use is prohibited; contact
  asas7412589630@gmail.com for a commercial licence. Chosen over MIT because the
  armour text is the part worth protecting, and an MIT grant would let anyone
  resell it.
- **Contact** for bug reports, feature requests and security issues:
  asas7412589630@gmail.com (or a GitHub issue / private advisory).
- **Roadmap** published in the README: more model families, console i18n,
  and a longer-term move to a website and desktop application.

### Known limitations

- `_forward_locked` is 2,149 lines in one function. Splitting it needs a golden
  test recording real upstream traffic first; a refactor without one has already
  cost a debugging cycle.
- Log rotation covers the request log only. The console trace and debug files grow
  unbounded in non-ephemeral mode.
- The console is Traditional Chinese only.
- WAF-blocked egress IPs are not fixable from inside the proxy.

---

Made by yuangeluyou.com :)
