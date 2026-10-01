"""Persistencia de keys, créditos, ban y usuarios del bot."""
from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
STORE_PATH = ROOT / "profiles" / "bot_data.json"  # dentro del volume Railway /app/profiles
ACTIVATION_COST_MXN = 120

_lock = threading.Lock()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load() -> dict[str, Any]:
    if STORE_PATH.exists():
        try:
            data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"keys": {}, "users": {}}
            data.setdefault("keys", {})
            data.setdefault("users", {})
            return data
        except (OSError, json.JSONDecodeError):
            pass
    return {"keys": {}, "users": {}}


def _save(data: dict[str, Any]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _key_code() -> str:
    return secrets.token_hex(4).upper()


def _user_entry(data: dict[str, Any], user_id: int) -> dict[str, Any]:
    uid = str(user_id)
    return data["users"].setdefault(
        uid,
        {"credits": 0, "history": [], "banned": False, "ban_reason": ""},
    )


class StoreError(Exception):
    pass


def get_credits(user_id: int) -> int:
    with _lock:
        data = _load()
        return int(data["users"].get(str(user_id), {}).get("credits", 0))


def add_credits(user_id: int, amount: int, reason: str = "") -> int:
    with _lock:
        data = _load()
        user = _user_entry(data, user_id)
        user["credits"] = int(user.get("credits", 0)) + int(amount)
        user.setdefault("history", []).append(
            {"ts": _now(), "delta": int(amount), "reason": reason, "balance": user["credits"]}
        )
        # mantener historial razonable
        if len(user["history"]) > 200:
            user["history"] = user["history"][-200:]
        _save(data)
        return int(user["credits"])


def set_credits(user_id: int, amount: int, reason: str = "admin_set") -> int:
    """Fija el saldo exacto (admin)."""
    amount = max(0, int(amount))
    with _lock:
        data = _load()
        user = _user_entry(data, user_id)
        prev = int(user.get("credits", 0))
        delta = amount - prev
        user["credits"] = amount
        user.setdefault("history", []).append(
            {"ts": _now(), "delta": delta, "reason": reason, "balance": amount}
        )
        if len(user["history"]) > 200:
            user["history"] = user["history"][-200:]
        _save(data)
        return amount


def consume_credit(user_id: int, reason: str = "vinculacion") -> bool:
    with _lock:
        data = _load()
        user = _user_entry(data, user_id)
        if user.get("banned"):
            return False
        if int(user.get("credits", 0)) < 1:
            return False
        user["credits"] = int(user.get("credits", 0)) - 1
        user.setdefault("history", []).append(
            {"ts": _now(), "delta": -1, "reason": reason, "balance": user["credits"]}
        )
        if len(user["history"]) > 200:
            user["history"] = user["history"][-200:]
        _save(data)
        return True


def refund_credit(user_id: int, reason: str = "reembolso_red") -> int:
    return add_credits(user_id, 1, reason)


def is_banned(user_id: int) -> bool:
    with _lock:
        data = _load()
        return bool(data["users"].get(str(user_id), {}).get("banned", False))


def ban_user(user_id: int, reason: str = "", banned_by: int | None = None) -> None:
    with _lock:
        data = _load()
        user = _user_entry(data, user_id)
        user["banned"] = True
        user["ban_reason"] = reason or "ban_admin"
        user["banned_at"] = _now()
        if banned_by is not None:
            user["banned_by"] = banned_by
        user.setdefault("history", []).append(
            {
                "ts": _now(),
                "delta": 0,
                "reason": f"ban:{reason or 'admin'}",
                "balance": int(user.get("credits", 0)),
            }
        )
        _save(data)


def unban_user(user_id: int, reason: str = "") -> None:
    with _lock:
        data = _load()
        user = _user_entry(data, user_id)
        user["banned"] = False
        user["ban_reason"] = ""
        user["unbanned_at"] = _now()
        user.setdefault("history", []).append(
            {
                "ts": _now(),
                "delta": 0,
                "reason": f"unban:{reason or 'admin'}",
                "balance": int(user.get("credits", 0)),
            }
        )
        _save(data)


def list_users(limit: int = 50) -> list[dict[str, Any]]:
    """Usuarios conocidos (con créditos, ban o historial)."""
    with _lock:
        data = _load()
        items: list[dict[str, Any]] = []
        for uid, meta in data["users"].items():
            items.append(
                {
                    "user_id": uid,
                    "credits": int(meta.get("credits", 0)),
                    "banned": bool(meta.get("banned", False)),
                    "ban_reason": meta.get("ban_reason") or "",
                    "history_n": len(meta.get("history") or []),
                }
            )
        items.sort(key=lambda x: (-x["credits"], x["user_id"]))
        return items[: max(1, limit)]


def get_user_info(user_id: int) -> dict[str, Any]:
    with _lock:
        data = _load()
        meta = data["users"].get(str(user_id), {})
        return {
            "user_id": str(user_id),
            "credits": int(meta.get("credits", 0)),
            "banned": bool(meta.get("banned", False)),
            "ban_reason": meta.get("ban_reason") or "",
            "history": list(meta.get("history") or [])[-15:],
        }


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
        if bool(data["users"].get(str(user_id), {}).get("banned", False)):
            raise StoreError("Tu cuenta está bloqueada. Contacta al admin @reddit61.")
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
        user = _user_entry(data, user_id)
        user["credits"] = int(user.get("credits", 0)) + uses
        user.setdefault("history", []).append(
            {
                "ts": _now(),
                "delta": uses,
                "reason": f"canje_key:{code}",
                "balance": user["credits"],
            }
        )
        if len(user["history"]) > 200:
            user["history"] = user["history"][-200:]
        _save(data)
        return uses


def list_keys(limit: int = 20) -> list[dict[str, Any]]:
    with _lock:
        data = _load()
        items = []
        for code, meta in sorted(
            data["keys"].items(), key=lambda x: x[1].get("created_at", ""), reverse=True
        ):
            items.append({"code": code, **meta})
            if len(items) >= limit:
                break
        return items
