"""Persistencia de keys, créditos y usuarios del bot."""
from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
STORE_PATH = ROOT / "bot_data.json"
ACTIVATION_COST_MXN = 15

_lock = threading.Lock()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load() -> dict[str, Any]:
    if STORE_PATH.exists():
        return json.loads(STORE_PATH.read_text(encoding="utf-8"))
    return {"keys": {}, "users": {}}


def _save(data: dict[str, Any]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _key_code() -> str:
    return secrets.token_hex(4).upper()


class StoreError(Exception):
    pass


def get_credits(user_id: int) -> int:
    with _lock:
        data = _load()
        return int(data["users"].get(str(user_id), {}).get("credits", 0))


def add_credits(user_id: int, amount: int, reason: str = "") -> int:
    with _lock:
        data = _load()
        uid = str(user_id)
        user = data["users"].setdefault(uid, {"credits": 0, "history": []})
        user["credits"] = int(user.get("credits", 0)) + amount
        user.setdefault("history", []).append(
            {"ts": _now(), "delta": amount, "reason": reason, "balance": user["credits"]}
        )
        _save(data)
        return user["credits"]


def consume_credit(user_id: int, reason: str = "vinculacion") -> bool:
    with _lock:
        data = _load()
        uid = str(user_id)
        user = data["users"].setdefault(uid, {"credits": 0, "history": []})
        if int(user.get("credits", 0)) < 1:
            return False
        user["credits"] -= 1
        user.setdefault("history", []).append(
            {"ts": _now(), "delta": -1, "reason": reason, "balance": user["credits"]}
        )
        _save(data)
        return True


def refund_credit(user_id: int, reason: str = "reembolso_red") -> int:
    return add_credits(user_id, 1, reason)


def admin_generate_key(uses: int, created_by: int) -> str:
    if uses < 1 or uses > 100:
        raise StoreError("Los usos deben estar entre 1 y 100.")
    with _lock:
        data = _load()
        code = _key_code()
        while code in data["keys"]:
            code = _key_code()
        data["keys"][code] = {
            "uses_total": uses,
            "redeemed_by": None,
            "redeemed_at": None,
            "created_at": _now(),
            "created_by": created_by,
            "status": "active",
        }
        _save(data)
        return code


def redeem_key(user_id: int, code: str) -> int:
    code = (code or "").strip().upper()
    if not code:
        raise StoreError("Key vacía.")
    with _lock:
        data = _load()
        key = data["keys"].get(code)
        if not key:
            raise StoreError("Key inválida o inexistente.")
        if key.get("redeemed_by") is not None:
            raise StoreError("Esta key ya fue canjeada y no se puede usar de nuevo.")
        if key.get("status") != "active":
            raise StoreError("Key expirada o desactivada.")
        uses = int(key["uses_total"])
        key["redeemed_by"] = user_id
        key["redeemed_at"] = _now()
        key["status"] = "redeemed"
        uid = str(user_id)
        user = data["users"].setdefault(uid, {"credits": 0, "history": []})
        user["credits"] = int(user.get("credits", 0)) + uses
        user.setdefault("history", []).append(
            {
                "ts": _now(),
                "delta": uses,
                "reason": f"canje_key:{code}",
                "balance": user["credits"],
            }
        )
        _save(data)
        return uses


def list_keys(limit: int = 20) -> list[dict[str, Any]]:
    with _lock:
        data = _load()
        items = []
        for code, meta in sorted(data["keys"].items(), key=lambda x: x[1].get("created_at", ""), reverse=True):
            items.append({"code": code, **meta})
            if len(items) >= limit:
                break
        return items
