# Contributing

Short version: keep it standard-library only, keep the comment style, and prove
behaviour changes with a measurement rather than an opinion.

---

## Ground rules

**1 · Standard library only.**
No `pip install`, in code or in tests. The proxy is meant to run from a bare
`python3` on a phone. CI enforces this — a third-party import fails the build.

**2 · Tests must be hermetic.**
A suite that needs a real provider is a suite that fails in CI for reasons
unrelated to the change. Point the proxy at a local fake instead:

```python
env.update({
    "ARMOR_UPSTREAM_HOST": "127.0.0.1",
    "ARMOR_UPSTREAM_PORT": str(FAKE_PORT),
    "ARMOR_UPSTREAM_HTTPS": "0",
    "ARMOR_PROXY_PORT": str(PROXY_PORT),
    "ARMOR_BASE": HERE,
})
```

`test_concurrency.py` and `test_sources_integration.py` are the reference
implementations of this pattern.

**3 · Comments explain why, not what.**
Look at any existing block: nearly all of them record a measurement, an incident,
or a rejected alternative. `# increment the counter` is noise; `# 401 was retried
for the whole budget on one dead key — the fix is the bound, not the ban` is the
reason the line exists.

**4 · Never delete the operator's message.**
Anything that filters, scrubs or rewrites history must be tested against the case
where the thing being filtered *is* the request. That exact bug has happened.

---

## Testing

```sh
./run-tests.sh                 # everything that can run offline
python3 test_scrub.py          # one suite
```

Two suites disagree about how to select offline mode. That is a known wart:

```sh
python3 test_clients.py --offline              # a CLI flag
TRIAD_OFFLINE=1 python3 test_concurrency.py 4  # an env var; 4 = concurrency
```

Passing the wrong one gives a *false failure*, not an error message — you will
spend twenty minutes debugging code you did not break.

Adding a test: append to the existing per-area suite rather than making a new
file. `test_scrub.py` is the model — each check prints `got/want` and the file
returns non-zero on any mismatch.

---

## Changing the armour

`blocks/` is data, not code. To swap in your own prompt:

1. Put the text in `blocks/` (any `.txt`).
2. Register it in `blocks/registry.json` under a profile.
3. Point model families at that profile.
4. `./triad-up --restart`

Confirm what a model will actually receive before and after:

```sh
curl 'http://127.0.0.1:8412/armor?model=glm-5.3&level=native'
```

Keep `.gitattributes`'s `blocks/*.txt diff` attribute in mind: the blocks are
tracked as prose so a prompt change is reviewable as a word diff.

Since the armour is a policy choice rather than a technical one, PRs that swap
the default blocks wholesale will not be merged — add a *profile* instead, and
leave the default alone so existing installs do not change behaviour on upgrade.

---

## Pull requests

- One concern per PR. A security fix and a refactor together cannot be reviewed.
- Run `./run-tests.sh` before pushing. CI runs the same thing.
- If the change is behavioural, say what you measured. A number beats an argument.
- If it is a bug fix, add the regression test in the same commit.

Commit messages: imperative mood, present tense, one line under ~72 characters,
and a body when the *why* is not obvious.

```
Bound the 401 retry to one pass over the key pool

Removing 401 from the retry list entirely broke rotation: with one
revoked key among two working ones, every request 502'd. The bug was
that a single dead key consumed the whole 90s budget before the caller
heard "auth", not that rotation happened at all.
```

---

## Licensing of contributions

This project is released under **CC BY-NC 4.0** (Attribution-NonCommercial).
By opening a pull request you agree that your contribution is licensed under the
same terms.

That means, concretely: **the project cannot be used commercially**, and a
contribution cannot be relicensed for commercial use without your explicit
agreement. If you are not comfortable with that, please open an issue describing
the change instead of a pull request.

---

## Reporting bugs

Include the version (or commit), platform, and the smallest reproduction you can
manage. For anything touching auth, CORS or the setup flow, use the private
advisory channel in [SECURITY.md](SECURITY.md) instead of a public issue.

Diagnostics that help:

```sh
./triad-up --status                          # port, pid, healthz
tail -50 logs/triad.log                      # what the proxy decided
curl -D- localhost:8412/healthz -o /dev/null # headers, including X-Armor-*
```

---

Made by yuangeluyou.com :)
