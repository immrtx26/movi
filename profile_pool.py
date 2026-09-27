from __future__ import annotations

import json
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROFILES_DIR = ROOT / "profiles" / "movistar_perfiles"

MAX_SUCCESSES_PER_PROFILE = 10

_lock = threading.Lock()
_holds: dict[int, str] = {}


def _ensure_pool():
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)


def _profile_dir(pid: str) -> Path:
    return PROFILES_DIR / str(pid)


def _load_config(pid: str) -> dict:
    cfg = _profile_dir(pid) / "config.json"
    if cfg.exists():
        try:
            return json.loads(cfg.read_text("utf-8"))
        except Exception:
            pass
    return {}


def _save_config(pid: str, data: dict):
    (_profile_dir(pid) / "config.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _active_profiles():
    _ensure_pool()
    return sorted(
        [p for p in PROFILES_DIR.iterdir() if p.is_dir() and p.name.isdigit()],
        key=lambda p: int(p.name),
    )


def startup_report() -> str:
    profs = _active_profiles()
    return (
        "Preparación de perfiles:\n\n"
        f"Perfiles en {PROFILES_DIR}\n"
        f"Activos: {len(profs)} / {len(profs)} (máx {MAX_SUCCESSES_PER_PROFILE} éxitos c/u)\n"
        "Descartados: 0"
    )


def get_profile(profile_id: str):
    p = _profile_dir(profile_id)
    if not p.exists():
        return None
    return {
        "id": profile_id,
        "path": p,
        "config": _load_config(profile_id),
    }


def profile_status(profile_id: str):
    cfg = _load_config(profile_id)
    return {
        "successes": cfg.get("successes", 0),
        "max": MAX_SUCCESSES_PER_PROFILE,
        "held": profile_id in _holds.values(),
    }


def acquire_next_profile(user_id: int):
    with _lock:
        if user_id in _holds:
            return get_profile(_holds[user_id])

        for p in _active_profiles():
            pid = p.name
            cfg = _load_config(pid)
            if cfg.get("successes", 0) >= MAX_SUCCESSES_PER_PROFILE:
                continue
            if pid in _holds.values():
                continue

            _holds[user_id] = pid
            return {
                "id": pid,
                "path": p,
                "config": cfg,
            }

    return None


def release_profile(user_id: int):
    with _lock:
        _holds.pop(user_id, None)


def release_all_for_user(user_id: int):
    release_profile(user_id)


def user_holds_profile(user_id: int):
    return _holds.get(user_id)


def record_profile_success(profile_id: str):
    cfg = _load_config(profile_id)
    cfg["successes"] = cfg.get("successes", 0) + 1
    _save_config(profile_id, cfg)
    return cfg["successes"]


def discard_profile(profile_id: str, reason: str = ""):
    cfg = _load_config(profile_id)
    cfg["discarded"] = True
    cfg["reason"] = reason
    _save_config(profile_id, cfg)


def maybe_purge_exhausted_profile(profile_id: str):
    cfg = _load_config(profile_id)
    if cfg.get("successes", 0) >= MAX_SUCCESSES_PER_PROFILE:
        discard_profile(profile_id, "Límite alcanzado")
