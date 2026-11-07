#!/usr/bin/env python3
"""triad.py — Cross-platform manager for the POPO-V1 / Triad armor proxy.

Supports Windows, Linux, and macOS.
Usage:
  python triad.py               Start proxy (or report status if running)
  python triad.py --status      Check if proxy is running and responsive
  python triad.py --restart     Restart proxy (wait for drain and release port)
  python triad.py --stop        Stop running proxy
  python triad.py --foreground  Run in foreground (logs directly to console)
"""
import os
import sys
import time
import signal
import socket
import urllib.request
import subprocess

DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("TRIAD_PORT", "8412"))
PID_FILE = os.path.join(DIR, "triad.pid")
EPHEMERAL = os.environ.get("ARMOR_EPHEMERAL", "0") == "1"


def get_health(timeout=2.0):
    url = f"http://127.0.0.1:{PORT}/healthz"
    req = urllib.request.Request(url, headers={"User-Agent": "triad-cli"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return True
    except Exception:
        pass
    return False


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        success = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(success and exit_code.value == STILL_ACTIVE)
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def kill_pid(pid: int, sig=signal.SIGTERM):
    if not is_pid_alive(pid):
        return
    if os.name == "nt":
        # On Windows, try taskkill gracefully first, or fallback to TerminateProcess
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
    else:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def get_running_pid():
    if os.path.isfile(PID_FILE):
        try:
            with open(PID_FILE, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
                if is_pid_alive(pid):
                    return pid
        except Exception:
            pass
    return None


def status_cmd():
    pid = get_running_pid()
    if pid:
        alive = get_health(2.0)
        status_msg = "healthy" if alive else "unresponsive"
        print(f"running pid={pid} port={PORT} healthz={status_msg}")
        return 0
    # Also check if port is listening even without pidfile
    if get_health(1.0):
        print(f"running port={PORT} (pid unknown) healthz=healthy")
        return 0
    print("not running")
    return 1


def stop_cmd():
    pid = get_running_pid()
    if not pid:
        print("not running")
        if os.path.isfile(PID_FILE):
            try:
                os.remove(PID_FILE)
            except OSError:
                pass
        return 0
    print(f"stopping pid={pid}...")
    kill_pid(pid)
    for _ in range(60):
        if not is_pid_alive(pid):
            break
        time.sleep(0.2)
    if is_pid_alive(pid):
        print(f"force killing pid={pid}...")
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"], capture_output=True)
        else:
            kill_pid(pid, signal.SIGKILL)
    if os.path.isfile(PID_FILE):
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
    print("stopped")
    return 0


def prepare_dirs():
    if EPHEMERAL:
        for d in ["runtime/state", "artifacts", "state"]:
            os.makedirs(os.path.join(DIR, d), exist_ok=True)
    else:
        for d in ["logs", "runtime/state", "runtime/trace", "artifacts", "state", "media"]:
            os.makedirs(os.path.join(DIR, d), exist_ok=True)


def build_env():
    env = dict(os.environ)
    env.update({
        "ARMOR_PROXY_PORT": str(PORT),
        "ARMOR_BIND": "127.0.0.1",
        "ARMOR_BASE": DIR,
        "ARMOR_MAP_FILE": os.path.join(DIR, "policy", "source", "armor_map_active.json"),
        "ARMOR_ALIAS_FILE": os.path.join(DIR, "policy", "source", "armor_alias.json"),
        "ARMOR_KEY_POOL": os.path.join(DIR, "proxy", "upstream_keys.txt"),
        "ARMOR_CLIENT_KEYS": os.path.join(DIR, "proxy", "client_keys.txt"),
        "ARMOR_STATE_FILE": os.path.join(DIR, "runtime", "state", "session_state.json"),
        "ARMOR_ARCHIVE_DIR": os.path.join(DIR, "runtime", "state"),
        "ARMOR_LOG_DIR": os.path.join(DIR, "logs"),
        "ARMOR_PANEL_FILE": os.path.join(DIR, "proxy", "panel_settings.json"),
        "ARMOR_MEDIA_DIR": os.path.join(DIR, "media"),
        "ARMOR_PARAMS": "1",
        "ARMOR_REFRAME": "1",
        "ARMOR_STATE": "0",
        "ARMOR_ARCHIVE": "0",
        "ARMOR_LEVEL": os.environ.get("ARMOR_LEVEL", "native"),
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def start_cmd(foreground=False):
    pid = get_running_pid()
    if pid:
        status_cmd()
        return 0

    prepare_dirs()
    env = build_env()
    proxy_script = os.path.join(DIR, "armor_proxy.py")

    if foreground:
        print(f"starting armor proxy on port {PORT} (foreground)...")
        try:
            return subprocess.run([sys.executable, proxy_script], env=env, cwd=DIR).returncode
        except KeyboardInterrupt:
            print("\nstopped")
            return 0

    log_path = os.devnull if EPHEMERAL else os.path.join(DIR, "logs", "triad.log")
    log_file = open(log_path, "a", encoding="utf-8", errors="replace")

    cflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(
        [sys.executable, proxy_script],
        env=env,
        cwd=DIR,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=cflags,
    )
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(proc.pid) + "\n")

    # Wait for liveness
    time.sleep(1.2)
    for _ in range(15):
        if get_health(0.5):
            break
        time.sleep(0.3)

    return status_cmd()


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg in ("--status", "status", "-s"):
        sys.exit(status_cmd())
    elif arg in ("--stop", "stop"):
        sys.exit(stop_cmd())
    elif arg in ("--restart", "restart", "-r"):
        stop_cmd()
        time.sleep(0.5)
        sys.exit(start_cmd(foreground=False))
    elif arg in ("--foreground", "-f", "run"):
        sys.exit(start_cmd(foreground=True))
    elif arg in ("", "up", "start"):
        sys.exit(start_cmd(foreground=False))
    elif arg == "acct":
        acct_script = os.path.join(DIR, "acct")
        sys.exit(subprocess.run([sys.executable, acct_script] + sys.argv[2:]).returncode)
    else:
        print(__doc__.strip(), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
