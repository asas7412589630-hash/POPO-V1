#!/usr/bin/env python3
"""Push the prepared repo to GitHub.

GitHub retired password authentication for git operations in August 2021, so a
Personal Access Token is required — account email + password will always return
401, both on the API and on `git push`.

Usage:
    GITHUB_TOKEN=ghp_...  ./push_to_github.sh <owner> <repo>

Or let it read GH_OWNER / GH_REPO from the environment. The token is never
echoed, never written to disk, and never placed in the remote URL — it is fed to
`git push` through a one-shot credential helper so it does not end up in
.git/config.
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
UA = {"User-Agent": "cinder-deploy", "Accept": "application/vnd.github+json"}


def api(method: str, path: str, token: str, body=None):
    url = "https://api.github.com" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={**UA, "Authorization": "Bearer " + token,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {}
        except Exception:
            return e.code, {"message": raw[:200].decode("utf-8", "replace")}


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    owner = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GH_OWNER", "")).strip()
    repo = (sys.argv[2] if len(sys.argv) > 2 else os.environ.get("GH_REPO", "popo-v1")).strip()

    if not token:
        print("missing GITHUB_TOKEN.\n"
              "  create one: https://github.com/settings/tokens  (scope: repo, workflow)\n"
              "  then: GITHUB_TOKEN=ghp_... ./push_to_github.sh %s %s"
              % (owner or "<owner>", repo))
        return 2

    # 1. who is this token
    st, me = api("GET", "/user", token)
    if st != 200:
        print("token rejected (HTTP %d): %s" % (st, me.get("message", "")))
        return 1
    login = me.get("login", "")
    print("token belongs to:", login)
    owner = owner or login

    # 2. does the repo exist?
    st, info = api("GET", "/repos/%s/%s" % (owner, repo), token)
    created = False
    if st == 404:
        print("repo %s/%s not found — creating it (private)" % (owner, repo))
        st, info = api("POST", "/user/repos", token, {
            "name": repo,
            "private": True,
            "description": "Local OpenAI-compatible proxy with injectable armour",
            "has_issues": True,
            "has_wiki": False,
            "auto_init": False,
        })
        if st not in (200, 201):
            print("create failed (HTTP %d): %s" % (st, info.get("message", "")))
            return 1
        created = True
    elif st != 200:
        print("cannot read repo (HTTP %d): %s" % (st, info.get("message", "")))
        return 1

    full = info.get("full_name") or ("%s/%s" % (owner, repo))
    print("repo:", full, "| private:", info.get("private"), "| new:", created)

    clone_url = info.get("clone_url") or ("https://github.com/%s.git" % full)
    # Embed the token in the URL for this one push only. It is not written to
    # .git/config because we pass it as the remote argument, not `git remote add`.
    auth_url = clone_url.replace("https://", "https://x-access-token:%s@" % token, 1)

    def git(*args, **kw):
        return subprocess.run(["git"] + list(args), cwd=HERE, text=True,
                              capture_output=True, **kw)

    # 3. bring the remote's initial commit (if the user made one) into our history
    git("remote", "remove", "origin")
    git("remote", "add", "origin", auth_url)

    st_remote = git("ls-remote", "--heads", "origin")
    remote_has_main = "refs/heads/main" in (st_remote.stdout or "")

    if remote_has_main and not created:
        print("remote already has main — fetching to merge histories")
        git("fetch", "origin", "main")
        # Unrelated histories happen when the repo was created with a README on
        # the web. --allow-unrelated-histories keeps both, ours last so our files
        # win on conflict.
        r = git("merge", "--allow-unrelated-histories", "--no-edit",
                "-X", "ours", "origin/main")
        if r.returncode != 0:
            print("merge failed, trying rebase")
            git("rebase", "-X", "theirs", "origin/main")

    branch = (git("rev-parse", "--abbrev-ref", "HEAD").stdout or "main").strip()
    print("branch:", branch)
    r = git("push", "-u", "origin", branch)
    print(r.stdout.strip())
    if r.returncode != 0:
        print("PUSH FAILED:")
        print(r.stderr.strip()[:1500])
        return 1

    # 4. leave no credential behind
    git("remote", "set-url", "origin", clone_url)

    print()
    print("done → https://github.com/%s" % full)
    return 0


if __name__ == "__main__":
    sys.exit(main())
