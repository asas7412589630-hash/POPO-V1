"""Open a public API tunnel and publish its verified URL to POPO."""
from __future__ import annotations
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

DATA = Path(os.environ.get("POPO_DATA_DIR", "data")).resolve()
GATEWAY_PORT = int(os.environ.get("POPO_GATEWAY_PORT", "18423"))
URL_FILE = DATA / "tunnel_url.txt"
URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.lhr\.life")


def publish_url(url: str) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    temporary = URL_FILE.with_suffix(".txt.tmp")
    temporary.write_text(url, encoding="utf-8")
    os.replace(temporary, URL_FILE)


def main() -> int:
    ssh = shutil.which("ssh")
    if not ssh:
        print("ssh executable not found", file=sys.stderr, flush=True)
        return 2
    command = [ssh, "-o", "StrictHostKeyChecking=accept-new",
               "-o", "ServerAliveInterval=60", "-o", "ExitOnForwardFailure=yes",
               "-R", f"80:127.0.0.1:{GATEWAY_PORT}", "nokey@localhost.run"]
    print(f"Starting public API tunnel to 127.0.0.1:{GATEWAY_PORT}", flush=True)
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", bufsize=1)
    except OSError as exc:
        print(f"Could not start SSH tunnel: {exc}", file=sys.stderr, flush=True)
        return 2
    try:
        assert process.stdout is not None
        for line in process.stdout:
            match = URL_PATTERN.search(line)
            if match:
                publish_url(match.group(0))
                print(f"PUBLIC_URL_UPDATED={match.group(0)}", flush=True)
            elif any(term in line.lower() for term in (
                    "permission denied", "remote port forwarding failed",
                    "connection closed", "could not resolve", "error:", "fatal:")):
                print(line.rstrip(), file=sys.stderr, flush=True)
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        try:
            return process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
