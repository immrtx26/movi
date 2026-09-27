from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

log = logging.getLogger("movistar-bot")

ROOT = Path(__file__).resolve().parent
PROFILES_DIR = ROOT / "profiles" / "movistar_perfiles"

MAX_SUCCESSES_PER_PROFILE = 10

_lock = Lock()


@dataclass
class Profile:
    id: str
    folder: Path
    label: str
    uploaded_by: int | None
    successes: int = 0
    in_use_by: int | None = None
    discarded: bool = False


_profiles: dict[str, Profile] = {}


def _load_profile(folder: Path) -> Profile | None:
    if not folder.is_dir():
        return None

    front = folder / "front.jpg"
    back = folder / "back.jpg"
    selfie = folder / "selfie.jpg"

    if not (front.exists() and back.exists() and selfie.exists()):
        return None

    cfg = folder / "config.json"

    label = f"Perfil {folder.name}"
    uploaded_by = None
    successes = 0

    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            label = data.get("label", label)
            uploaded_by = data.get("uploaded_by")
            successes = int(data.get("successes", 0))
        except Exception:
            pass

    return Profile(
        id=folder.name,
        folder=folder,
        label=label,
        uploaded_by=uploaded_by,
        successes=successes,
    )


def prepare_pool() -> None:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    with _lock:
        _profiles.clear()

        for folder in sorted(PROFILES_DIR.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else 999999):
            profile = _load_profile(folder)
            if profile:
                _profiles[profile.id] = profile

    log.info(startup_report())


def startup_report() -> str:
    with _lock:
        activos = sum(1 for p in _profiles.values() if not p.discarded)
        descartados = sum(1 for p in _profiles.values() if p.discarded)

    return (
        "Perfiles en %s\n"
        "  Activos: %s / %s (máx %s éxitos c/u)\n"
        "  Descartados: %s"
    ) % (
        PROFILES_DIR,
        activos,
        len(_profiles),
        MAX_SUCCESSES_PER_PROFILE,
        descartados,
    )


def acquire_next_profile(user_id: int) -> Profile | None:
    with _lock:
        disponibles = [
            p for p in _profiles.values()
            if not p.discarded
            and p.in_use_by is None
            and p.successes < MAX_SUCCESSES_PER_PROFILE
        ]

        if not disponibles:
            return None

        perfil = min(disponibles, key=lambda p: p.successes)
        perfil.in_use_by = user_id
        return perfil


def release_profile(profile_id: str) -> None:
    with _lock:
        p = _profiles.get(profile_id)
        if p:
            p.in_use_by = None


def release_all_for_user(user_id: int) -> None:
    with _lock:
        for p in _profiles.values():
            if p.in_use_by == user_id:
                p.in_use_by = None


def record_profile_success(profile_id: str) -> None:
    with _lock:
        p = _profiles.get(profile_id)
        if not p:
            return

        p.successes += 1
        p.in_use_by = None

        cfg = p.folder / "config.json"
        data = {}

        if cfg.exists():
            try:
                data = json.loads(cfg.read_text(encoding="utf-8"))
            except Exception:
                data = {}

        data["label"] = p.label
        data["uploaded_by"] = p.uploaded_by
        data["successes"] = p.successes

        cfg.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        if p.successes >= MAX_SUCCESSES_PER_PROFILE:
            p.discarded = True


def discard_profile(profile_id: str, reason: str = "") -> None:
    with _lock:
        p = _profiles.get(profile_id)
        if p:
            p.discarded = True
            p.in_use_by = None

    if reason:
        log.warning("Perfil %s descartado: %s", profile_id, reason)


def maybe_purge_exhausted_profile(profile_id: str) -> None:
    with _lock:
        p = _profiles.get(profile_id)
        if p and p.successes >= MAX_SUCCESSES_PER_PROFILE:
            p.discarded = True


def get_profile(profile_id: str) -> Profile | None:
    with _lock:
        return _profiles.get(profile_id)


def user_holds_profile(user_id: int) -> bool:
    with _lock:
        return any(p.in_use_by == user_id for p in _profiles.values())


def profile_status() -> str:
    with _lock:
        if not _profiles:
            return "No hay perfiles cargados."

        lineas = []
        for p in sorted(_profiles.values(), key=lambda x: int(x.id)):
            estado = "Descartado" if p.discarded else "Disponible"
            if p.in_use_by:
                estado = f"En uso ({p.in_use_by})"

            lineas.append(
                f"{p.id}. {p.label} | {estado} | {p.successes}/{MAX_SUCCESSES_PER_PROFILE}"
            )

        return "\n".join(lineas)


def is_profile_busy(profile_id, ignore_user_id=None):
    with _lock:
        p=_profiles.get(str(profile_id))
        return bool(p and p.in_use_by is not None and (ignore_user_id is None or p.in_use_by!=ignore_user_id))
