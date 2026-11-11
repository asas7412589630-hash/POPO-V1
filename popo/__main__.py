"""Entry point: ``python3 -m popo``.

Subcommands follow 1.x so existing scripts keep working:

    python3 -m popo                # start the proxy (same as 1.x)
    python3 -m popo --check        # validate configuration, list routes
    python3 -m popo --status       # report whether it is listening
    python3 -m popo --version
"""
from __future__ import annotations
import sys


def _check() -> int:
    from popo.config import get_config
    from popo.server.routes import ROUTES
    config = get_config()
    print(f"POPO {config.version}")
    print(f"bind       {config.bind_host}:{config.bind_port}")
    print(f"upstream   {config.upstream_host or '(not configured)'}")
    print(f"key        {'set' if config.upstream_key else 'missing'}")
    print(f"armor      {config.armor_enabled}  scrub {config.scrub_enabled}")
    print(f"ephemeral  {config.ephemeral}")
    print(f"routes     {len(ROUTES)}")
    for route in ROUTES:
        print(f"  {route.method:4s} {route.path:32s} {route.auth}")
    problems = []
    if not config.upstream_host:
        problems.append("no upstream_host (run setup at /app)")
    if not config.upstream_key:
        problems.append("no upstream_key")
    if config.bind_host not in ("127.0.0.1", "::1") and not config.require_client_key:
        problems.append("public bind with require_client_key=false is an open relay")
    for problem in problems:
        print(f"warning: {problem}")
    return 0


def _status() -> int:
    import socket
    from popo.config import get_config
    config = get_config()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        if sock.connect_ex((config.bind_host if config.bind_host != "0.0.0.0" else "127.0.0.1",
                            config.bind_port)) == 0:
            print(f"listening on {config.bind_host}:{config.bind_port}")
            return 0
        print("not listening")
        return 1
    finally:
        sock.close()


def _tunnel() -> int:
    """Report the tunnel URL, or say there is none. Never invent one."""
    import os
    from popo.server.deploy import discover_tunnel
    candidates = []
    env_url = os.environ.get("POPO_PUBLIC_BASE", "").strip()
    if env_url:
        candidates.append((env_url, "POPO_PUBLIC_BASE"))
    for path, label in (("data/tunnel_url.txt", "tunnel_url.txt"),
                        (os.path.expanduser("~/.cloudflared/url"), "cloudflared")):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                candidates.append((handle.read(), label))
        except OSError:
            continue
    found = discover_tunnel(candidates)
    if not found:
        print("no verified tunnel URL")
        return 1
    print(f"{found.url} (from {found.source})")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv:
        from popo import __version__
        print(__version__)
        return 0
    if "--check" in argv:
        return _check()
    if "--status" in argv:
        return _status()
    if "--systemd" in argv:
        from popo.server.deploy import env_file_template, systemd_unit
        import os
        print(systemd_unit(workdir=os.getcwd()))
        print("# --- /etc/popo/popo.env ---")
        print(env_file_template())
        return 0
    if "--tunnel" in argv:
        return _tunnel()
    from popo.config import Config
    from popo.server.app import serve
    config = Config.from_env()
    config.load_install_state()
    serve(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
