#!/usr/bin/env python3
"""Regression: client keys and access mode.

Two properties, and both are security properties, not conveniences:

  1. a disabled key stops working IMMEDIATELY, without being deleted
  2. the raw token is returned exactly once and never again

The second is easy to break by accident — someone adds "show key" to the list
endpoint and the keyring becomes a credential printer. The test pins it.

Run: python3 test_keys.py
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

tmp = tempfile.mkdtemp(prefix="keys_test_")
os.environ["ARMOR_KEYS_JSON"] = os.path.join(tmp, "keys.json")
os.environ["ARMOR_CLIENT_KEYS"] = os.path.join(tmp, "client_keys.txt")
open(os.environ["ARMOR_CLIENT_KEYS"], "w", encoding="utf-8").write("triad-legacy-token-abc\n")

import keyring as K

fails = []


def check(name, got, want):
    ok = got == want
    print("  %-44s %-24r %s" % (name, got, "ok" if ok else "WANT %r" % (want,)))
    if not ok:
        fails.append(name)


print("1. an existing plain file is adopted, not ignored")
keys = K.list_keys()
check("legacy key imported", len(keys), 1)
check("legacy token still verifies", bool(K.verify("triad-legacy-token-abc")), True)
check("wrong token rejected", K.verify("triad-nope"), {})

print("2. creating a key returns the token exactly once")
rec = K.create("phone")
tok = rec["token"]
check("token has a prefix", tok.startswith(K.PREFIX), True)
check("token is long enough", len(tok) > 30, True)
check("record carries the token", "token" in rec, True)

listed = K.list_keys()
matched = [k for k in listed if k["id"] == rec["id"]]
check("record appears in the list", len(matched), 1)
check("list does NOT contain the token", "token" in (matched[0] if matched else {}), False)
check("only a prefix is exposed", matched[0]["prefix"].endswith("…"), True)

print("3. the token is not stored, only its hash")
raw = open(os.environ["ARMOR_KEYS_JSON"], encoding="utf-8").read()
check("raw token absent from disk", tok in raw, False)
check("hash present on disk", K._hash(tok) in raw, True)

print("4. disabling takes effect immediately")
check("works before disable", bool(K.verify(tok)), True)
K.set_enabled(rec["id"], False)
check("rejected after disable", K.verify(tok), {})
check("still listed (not deleted)", len([k for k in K.list_keys() if k["id"] == rec["id"]]), 1)
check("disabled key reported as disabled",
      [k for k in K.list_keys() if k["id"] == rec["id"]][0]["enabled"], False)

print("5. re-enabling restores it")
K.set_enabled(rec["id"], True)
check("works again", bool(K.verify(tok)), True)

print("6. the mirror file follows the enabled set")
mirror = open(os.environ["ARMOR_CLIENT_KEYS"], encoding="utf-8").read()
check("mirror contains the new token", tok in mirror, True)
K.set_enabled(rec["id"], False)
mirror = open(os.environ["ARMOR_CLIENT_KEYS"], encoding="utf-8").read()
check("disabled token removed from mirror", tok in mirror, False)
K.set_enabled(rec["id"], True)

print("7. delete removes the record and the mirror line")
check("deleted", K.delete(rec["id"]), True)
check("gone from list", len([k for k in K.list_keys() if k["id"] == rec["id"]]), 0)
check("token no longer verifies", K.verify(tok), {})
mirror = open(os.environ["ARMOR_CLIENT_KEYS"], encoding="utf-8").read()
check("removed from mirror", tok in mirror, False)

print("8. a surviving key keeps working after all of that")
check("legacy still valid", bool(K.verify("triad-legacy-token-abc")), True)

print("9. rename")
r2 = K.create("temp")
K.rename(r2["id"], "renamed")
check("label changed",
      [k for k in K.list_keys() if k["id"] == r2["id"]][0]["label"], "renamed")

print("10. last_used is recorded")
K.verify(r2["token"])
check("hits counted",
      [k for k in K.list_keys() if k["id"] == r2["id"]][0]["hits"] >= 1, True)

shutil.rmtree(tmp, ignore_errors=True)

print()
if fails:
    print("FAILED %d" % len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("ALL PASS")
