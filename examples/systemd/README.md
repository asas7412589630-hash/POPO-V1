# systemd

Two units, deliberately separate: the proxy and the tunnel fail independently,
and restarting one must not restart the other.

## proxy

```sh
sudo cp prism.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now prism
```

Edit `User=` and `WorkingDirectory=` first — the defaults are `ubuntu` and
`/home/ubuntu/prism`.

**Ephemeral mode is on in the shipped unit.** Nothing is written to disk. If you
want logs and archives, set `ARMOR_EPHEMERAL=0`.

## tunnel

Optional. Only needed when clients are not on the same host and you do not want
to use an SSH tunnel.

```sh
sudo cp prism-tunnel.service /etc/systemd/system/
```

Requires `cloudflared` on `PATH` and a tunnel already created:

```sh
cloudflared tunnel login
cloudflared tunnel create prism
cloudflared tunnel route dns prism api.example.com
```

The unit runs `triad-tunnel`, which re-reads the published hostname each start —
a quick tunnel changes its URL on every restart, and the cached one is what makes
clients see "service unavailable" against a perfectly healthy proxy.

## Checking

```sh
systemctl status prism
journalctl -u prism -f          # live
journalctl -u prism -n 100      # last 100 lines
```

In ephemeral mode the proxy's output goes to journald only — there is no
`logs/triad.log`. That is intentional: on a shared host, journald's retention and
access control are better than a file next to the keys.

## Binding

The shipped unit binds `127.0.0.1`. To serve other machines on the LAN, set
`ARMOR_BIND=0.0.0.0` **and make sure a client key exists first** — otherwise the
install is an open relay. `triad-deploy` refuses that combination; a hand-edited
unit does not.
