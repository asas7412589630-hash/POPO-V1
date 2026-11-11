"""Management views: keys, accounts, usage. Returns plain dicts."""

from __future__ import annotations
import json

from popo.auth.accounts import AccountStore
from popo.auth.gate import Identity
from popo.auth.keys import KeyStore
from popo.errors import ErrorKind, ProxyError
from popo.storage.records import RecordStore


def list_keys(store: KeyStore) -> dict:
    return {"keys": [
        {"key_id": record.key_id, "label": record.label, "enabled": record.enabled,
         "created_at": record.created_at, "last_used": record.last_used,
         "owner_id": record.owner_id, "allow_public": record.allow_public}
        for record in store.list_all()]}


def keys_action(store: KeyStore, body: bytes, identity: Identity) -> tuple[int, dict]:
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProxyError(ErrorKind.BAD_REQUEST, f"invalid JSON body: {exc}") from exc
    action = payload.get("action")
    if action == "create":
        label = str(payload.get("label", ""))[:80]
        scope = payload.get("scope", "internal")
        if scope not in ("internal", "public"):
            raise ProxyError(ErrorKind.BAD_REQUEST,
                             "scope must be internal or public")
        owner = identity.user_id if identity.via == "session" else ""
        token, record = store.create(label=label, owner_id=owner,
                                     allow_public=scope == "public")
        return 200, {"key": {"key_id": record.key_id, "token": token,
                             "label": record.label, "enabled": record.enabled,
                             "owner_id": record.owner_id,
                             "allow_public": record.allow_public,
                             "scope": "public" if record.allow_public else "internal"}}
    if action in ("disable", "delete"):
        key_id = str(payload.get("key_id", ""))
        if not key_id:
            raise ProxyError(ErrorKind.BAD_REQUEST, "key_id is required")
        if key_id == "admin" and action == "delete":
            raise ProxyError(ErrorKind.FORBIDDEN,
                             "deleting the admin key would lock the operator out")
        changed = store.disable(key_id) if action == "disable" else store.delete(key_id)
        if not changed:
            raise ProxyError(ErrorKind.NOT_FOUND, "key not found")
        return 200, {"ok": True, "action": action, "key_id": key_id}
    raise ProxyError(ErrorKind.BAD_REQUEST, "action must be create/disable/delete")


def list_accounts(store: AccountStore) -> dict:
    return {"accounts": [
        {"uid": account.uid, "username": account.username, "role": account.role,
         "disabled": account.disabled, "created_at": account.created_at}
        for account in store.list_all()]}


def accounts_action(store: AccountStore, body: bytes, identity: Identity) -> tuple[int, dict]:
    payload = json.loads(body.decode("utf-8") or "{}")
    action = payload.get("action")
    if action == "create":
        try:
            account = store.create(str(payload.get("username", "")),
                                   str(payload.get("password", "")),
                                   str(payload.get("role", "owner")))
        except ValueError as exc:
            raise ProxyError(ErrorKind.BAD_REQUEST, str(exc)) from exc
        return 200, {"account": {"uid": account.uid, "username": account.username,
                                 "role": account.role}}
    if action == "password":
        uid = str(payload.get("uid", ""))
        if not uid:
            raise ProxyError(ErrorKind.BAD_REQUEST, "uid is required")
        if not store.set_password(uid, str(payload.get("password", ""))):
            raise ProxyError(ErrorKind.NOT_FOUND, "account not found")
        return 200, {"ok": True}
    if action == "disable":
        uid = str(payload.get("uid", ""))
        if uid == identity.user_id:
            raise ProxyError(ErrorKind.FORBIDDEN,
                             "disabling your own account would lock you out")
        if not store.disable(uid):
            raise ProxyError(ErrorKind.NOT_FOUND, "account not found")
        return 200, {"ok": True}
    raise ProxyError(ErrorKind.BAD_REQUEST, "action must be create/password/disable")


def usage_summary(store: RecordStore, days: int = 7) -> dict:
    totals: dict[str, dict] = {}
    for row in store.read_usage(days=days):
        key = row.get("key_id") or "anonymous"
        entry = totals.setdefault(key, {"key_id": key, "requests": 0,
                                        "tokens_in": 0, "tokens_out": 0})
        entry["requests"] += 1
        entry["tokens_in"] += int(row.get("tokens_in") or 0)
        entry["tokens_out"] += int(row.get("tokens_out") or 0)
    return {"days": days, "by_key": sorted(totals.values(), key=lambda e: -e["requests"])}
