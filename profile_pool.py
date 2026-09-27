"""Pool de perfiles — API compatible con telegram_bot + profile_upload + enroll_automation."""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("movistar-bot")

ROOT = Path(__file__).resolve().parent
PROFILES_DIR = ROOT / "profiles" / "movistar_perfiles"
MAX_PROFILES = 250
MAX_SUCCESSES_PER_PROFILE = 10
LOCK_TTL_SECONDS = 900
USAGE_PATH = ROOT / "profile_usage.json"
MIN_IMAGE_BYTES = 2048
OCR_REQUIRED_KEYS = ("nombre", "curp", "clave_elector", "direccion")

_lock = threading.Lock()
_busy: dict[str, tuple[int, float]] = {}  # profile_id -> (user_id, expires_at)

FRENTE_NAMES = (
    "front.jpg", "frente.jpg", "FRENTE.jpeg", "FRENTE.jpg", "FRENTE.png",
    "frente.b64", "ine_frente.b64",
)
SELFIE_NAMES = (
    "selfie.jpg", "SELFIE.jpeg", "SELFIE.jpg", "SELFIE.png",
    "selfie.b64", "selfie_far.b64",
)
BACK_NAMES = ("back.jpg", "reverso.jpg", "REVERSO.png", "REVERSO.jpeg", "INE_BACK.jpeg")
OCR_NAMES = ("ocr.json", "ocr_data.json")
FAR_NAMES = ("far.png", "far.jpg", "selfie_far.b64", "far.b64")
CLOSE_NAMES = ("close.png", "close.jpg", "selfie_close.b64", "close.b64")


# ---------------------------------------------------------------------------
# Modelo Profile (campos que usa enroll_automation)
# ---------------------------------------------------------------------------

@dataclass
class Profile:
    id: str
    label: str
    frente_path: Path
    selfie_path: Path
    ocr_path: Path | None = None
    hubox_user: str = ""
    hubox_password: str = ""
    back_path: Path | None = None
    far_path: Path | None = None
    close_path: Path | None = None
    folder: Path | None = None  # compat con pool simplificado
    uploaded_by: int | None = None
    successes: int = 0
    in_use_by: int | None = None
    discarded: bool = False

    @property
    def root(self) -> Path:
        return self.frente_path.parent


@dataclass
class ProfileScan:
    id: str
    label: str
    folder: Path
    ready: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    profile: Profile | None = None


# ---------------------------------------------------------------------------
# Helpers de archivo / validación
# ---------------------------------------------------------------------------

def profiles_dir() -> Path:
    return PROFILES_DIR.resolve()


def _global_config() -> dict[str, Any]:
    path = PROFILES_DIR / "config.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"_config_error": str(exc)}


def _find_first(folder: Path, candidates: tuple[str, ...]) -> Path | None:
    for name in candidates:
        p = folder / name
        if p.is_file():
            return p
    if candidates and "ocr" in candidates[0]:
        matches = sorted(folder.glob("*ocr*.json"))
        if matches:
            return matches[0]
    return None


def _validate_file_exists(path: Path | None, label: str) -> list[str]:
    if path is None or not path.is_file():
        return [f"Falta archivo obligatorio: {label}"]
    return []


def _validate_image(path: Path, label: str) -> list[str]:
    errors: list[str] = []
    if not path.is_file():
        return [f"Falta {label}"]
    try:
        size = path.stat().st_size
    except OSError as exc:
        return [f"No se puede leer {label}: {exc}"]
    if size < MIN_IMAGE_BYTES:
        errors.append(f"{label} demasiado pequeño ({size} bytes, mínimo {MIN_IMAGE_BYTES})")
    try:
        header = path.read_bytes()[:8]
    except OSError as exc:
        return errors + [f"No se puede leer {label}: {exc}"]
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg"} and header[:2] != b"\xff\xd8":
        errors.append(f"{label} no parece un JPEG válido")
    elif ext == ".png" and header[:8] != b"\x89PNG\r\n\x1a\n":
        errors.append(f"{label} no parece un PNG válido")
    return errors


def _validate_ocr(path: Path | None) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    if path is None or not path.is_file():
        return None, [], ["Sin ocr.json local (Hubox extrae datos al vincular)"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"ocr.json ilegible o inválido: {exc}"], []
    if not isinstance(data, dict):
        return None, ["ocr.json debe ser un objeto JSON"], []
    errors: list[str] = []
    for key in OCR_REQUIRED_KEYS:
        val = data.get(key)
        if not val or not str(val).strip():
            errors.append(f"ocr.json sin campo obligatorio '{key}'")
    curp = str(data.get("curp", "")).strip()
    if curp and len(curp) != 18:
        errors.append(f"CURP inválida en ocr.json ({len(curp)} caracteres, se esperan 18)")
    return data, errors, []


def _label_from_folder(folder_id: str, local_cfg: dict[str, Any], global_cfg: dict[str, Any]) -> str:
    if local_cfg.get("label"):
        return str(local_cfg["label"])
    if folder_id in global_cfg.get("labels", {}):
        return str(global_cfg["labels"][folder_id])
    if folder_id.isdigit():
        sibling = _named_sibling_folder(folder_id)
        if sibling is not None and "_" in sibling.name:
            return sibling.name.split("_", 1)[1]
    if "_" in folder_id:
        return folder_id.split("_", 1)[1]
    return f"Perfil {folder_id}"


def _named_sibling_folder(numeric_id: str) -> Path | None:
    if not numeric_id.isdigit() or not PROFILES_DIR.is_dir():
        return None
    prefix = f"{numeric_id}_"
    matches = sorted(
        (p for p in PROFILES_DIR.iterdir() if p.is_dir() and p.name.startswith(prefix)),
        key=lambda p: p.name,
    )
    return matches[0] if matches else None


def _folder_sort_key(folder: Path) -> tuple[int, str]:
    prefix = folder.name.split("_", 1)[0]
    try:
        return int(prefix), folder.name
    except ValueError:
        return 9999, folder.name


def _is_numeric_pool_folder(folder: Path) -> bool:
    return folder.name.isdigit()


# ---------------------------------------------------------------------------
# Usage / locks
# ---------------------------------------------------------------------------

def _usage_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load_usage() -> dict[str, Any]:
    if USAGE_PATH.is_file():
        try:
            return json.loads(USAGE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"profiles": {}}


def _save_usage(data: dict[str, Any]) -> None:
    USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    USAGE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def profile_usage_info(profile_id: str) -> dict[str, Any]:
    entry = _load_usage().get("profiles", {}).get(str(profile_id), {})
    successes = int(entry.get("successes", 0))
    discarded = bool(entry.get("discarded", False))
    return {
        "successes": successes,
        "remaining": max(0, MAX_SUCCESSES_PER_PROFILE - successes),
        "discarded": discarded,
        "reason": entry.get("reason", ""),
    }


def is_profile_usable(profile_id: str) -> bool:
    return not profile_usage_info(profile_id)["discarded"]


def discard_profile(profile_id: str, reason: str = "") -> None:
    with _lock:
        data = _load_usage()
        profiles = data.setdefault("profiles", {})
        entry = profiles.setdefault(str(profile_id), {"successes": 0, "discarded": False})
        entry["discarded"] = True
        entry["reason"] = reason or f"Límite de {MAX_SUCCESSES_PER_PROFILE} vinculaciones"
        entry["discarded_at"] = _usage_now()
        if int(entry.get("successes", 0)) < MAX_SUCCESSES_PER_PROFILE:
            entry["successes"] = MAX_SUCCESSES_PER_PROFILE
        _save_usage(data)
        _busy.pop(str(profile_id), None)
    if reason:
        log.warning("Perfil %s descartado: %s", profile_id, reason)


def remove_profile(profile_id: str) -> bool:
    """Elimina perfil del pool: lock, usage y carpeta en disco."""
    pid = str(profile_id)
    folder = PROFILES_DIR / pid
    with _lock:
        _busy.pop(pid, None)
        data = _load_usage()
        profiles = data.setdefault("profiles", {})
        if pid in profiles:
            profiles.pop(pid, None)
            _save_usage(data)
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
    return not folder.exists()


def _purge_stale_locks() -> None:
    now = time.time()
    stale = [k for k, (_, exp) in _busy.items() if exp < now]
    for k in stale:
        _busy.pop(k, None)


def is_profile_busy(profile_id: str, *, ignore_user_id: int | None = None) -> bool:
    with _lock:
        _purge_stale_locks()
        holder = _busy.get(str(profile_id))
        if holder is None:
            return False
        if ignore_user_id is not None and holder[0] == ignore_user_id:
            return False
        return True


def record_profile_success(profile_id: str) -> int:
    with _lock:
        data = _load_usage()
        profiles = data.setdefault("profiles", {})
        entry = profiles.setdefault(str(profile_id), {"successes": 0, "discarded": False})
        if entry.get("discarded"):
            return int(entry.get("successes", MAX_SUCCESSES_PER_PROFILE))
        entry["successes"] = int(entry.get("successes", 0)) + 1
        count = entry["successes"]
        if count >= MAX_SUCCESSES_PER_PROFILE:
            entry["discarded"] = True
            entry["reason"] = f"{MAX_SUCCESSES_PER_PROFILE} vinculaciones exitosas"
            entry["discarded_at"] = _usage_now()
        _save_usage(data)
        _busy.pop(str(profile_id), None)
        return count


def release_profile(profile_id: str, user_id: int | None = None) -> None:
    """Libera lock. user_id opcional (compat telegram_bot simplificado)."""
    with _lock:
        pid = str(profile_id)
        holder = _busy.get(pid)
        if holder is None:
            return
        if user_id is not None and holder[0] != user_id:
            return
        _busy.pop(pid, None)


def release_all_for_user(user_id: int) -> None:
    with _lock:
        to_drop = [k for k, (uid, _) in _busy.items() if uid == user_id]
        for k in to_drop:
            _busy.pop(k, None)


def user_holds_profile(user_id: int) -> bool:
    with _lock:
        _purge_stale_locks()
        return any(uid == user_id for uid, _ in _busy.values())


# ---------------------------------------------------------------------------
# Scan / acquire
# ---------------------------------------------------------------------------

def scan_profile_folder(folder: Path, global_cfg: dict[str, Any] | None = None) -> ProfileScan:
    global_cfg = global_cfg or {}
    folder_id = folder.name
    local_cfg: dict[str, Any] = {}
    local_cfg_path = folder / "config.json"
    if local_cfg_path.is_file():
        try:
            local_cfg = json.loads(local_cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return ProfileScan(
                id=folder_id,
                label=folder_id,
                folder=folder,
                ready=False,
                errors=[f"config.json inválido: {exc}"],
            )

    label = _label_from_folder(folder_id, local_cfg, global_cfg)
    frente = _find_first(folder, FRENTE_NAMES)
    selfie = _find_first(folder, SELFIE_NAMES)
    ocr_file = _find_first(folder, OCR_NAMES)
    back = _find_first(folder, BACK_NAMES)
    far = _find_first(folder, FAR_NAMES)
    close = _find_first(folder, CLOSE_NAMES)

    hubox_user = str(local_cfg.get("hubox_user") or global_cfg.get("hubox_user") or "")
    hubox_password = str(local_cfg.get("hubox_password") or global_cfg.get("hubox_password") or "")

    errors: list[str] = []
    warnings: list[str] = []

    errors.extend(_validate_file_exists(frente, "front.jpg / frente"))
    errors.extend(_validate_file_exists(selfie, "selfie.jpg"))
    if frente:
        errors.extend(_validate_image(frente, "front"))
    if selfie:
        errors.extend(_validate_image(selfie, "selfie"))
    if back:
        errors.extend(_validate_image(back, "back"))
    else:
        warnings.append("Opcional: back.jpg no encontrado (el flujo genera QRs por OCR)")

    _ocr_data, ocr_errors, ocr_warnings = _validate_ocr(ocr_file)
    errors.extend(ocr_errors)
    warnings.extend(ocr_warnings)

    usage = profile_usage_info(folder_id)
    if usage["discarded"]:
        errors.append("Perfil descartado (agotado o marcado)")

    if errors:
        return ProfileScan(
            id=folder_id, label=label, folder=folder, ready=False,
            errors=errors, warnings=warnings,
        )

    assert frente is not None and selfie is not None
    if far is None or close is None:
        warnings.append("far/close 480x640 no pregenerados (se crearán al vincular)")

    uploaded_by = None
    try:
        if local_cfg.get("uploaded_by") is not None:
            uploaded_by = int(local_cfg["uploaded_by"])
    except (TypeError, ValueError):
        pass

    profile = Profile(
        id=folder_id,
        label=label,
        frente_path=frente.resolve(),
        selfie_path=selfie.resolve(),
        ocr_path=ocr_file.resolve() if ocr_file else None,
        hubox_user=hubox_user,
        hubox_password=hubox_password,
        back_path=back.resolve() if back else None,
        far_path=far.resolve() if far else None,
        close_path=close.resolve() if close else None,
        folder=folder.resolve(),
        uploaded_by=uploaded_by,
        successes=usage["successes"],
        discarded=usage["discarded"],
    )
    return ProfileScan(
        id=folder_id, label=label, folder=folder, ready=True,
        errors=[], warnings=warnings, profile=profile,
    )


def scan_all_profiles() -> list[ProfileScan]:
    base = PROFILES_DIR
    if not base.is_dir():
        return [
            ProfileScan(
                id="—",
                label="—",
                folder=base,
                ready=False,
                errors=[f"Carpeta no encontrada: {base.resolve()}"],
            )
        ]

    global_cfg = _global_config()
    scans: list[ProfileScan] = []
    if "_config_error" in global_cfg:
        scans.append(
            ProfileScan(
                id="config",
                label="config.json",
                folder=base,
                ready=False,
                errors=[f"config.json inválido: {global_cfg['_config_error']}"],
            )
        )
        global_cfg = {}

    all_folders = sorted(
        (p for p in base.iterdir() if p.is_dir() and not p.name.startswith("_")),
        key=_folder_sort_key,
    )
    for folder in all_folders:
        if not _is_numeric_pool_folder(folder):
            continue
        scans.append(scan_profile_folder(folder, global_cfg))
    return scans


def get_profile(profile_id: str) -> Profile | None:
    folder = PROFILES_DIR / str(profile_id)
    if not folder.is_dir():
        return None
    scan = scan_profile_folder(folder, _global_config())
    return scan.profile if scan.ready else None


def acquire_next_profile(user_id: int) -> Profile:
    """Toma el siguiente perfil libre. Lanza RuntimeError si no hay."""
    with _lock:
        _purge_stale_locks()
        candidates: list[Profile] = []
        for scan in scan_all_profiles():
            if not scan.ready or scan.profile is None:
                continue
            pid = scan.id
            if pid in _busy:
                continue
            usage = profile_usage_info(pid)
            if usage["discarded"] or usage["successes"] >= MAX_SUCCESSES_PER_PROFILE:
                continue
            candidates.append(scan.profile)

        if not candidates:
            raise RuntimeError(
                "No hay perfiles disponibles en el pool. "
                "Agrega perfiles o espera a que se liberen."
            )

        candidates.sort(key=lambda p: profile_usage_info(p.id)["successes"])
        chosen = candidates[0]
        _busy[chosen.id] = (int(user_id), time.time() + LOCK_TTL_SECONDS)
        chosen.in_use_by = int(user_id)
        return chosen


def profile_status() -> list[dict[str, Any]]:
    """Lista de dicts para /perfiles (API completa)."""
    items: list[dict[str, Any]] = []
    with _lock:
        _purge_stale_locks()
        busy_snapshot = dict(_busy)
    for scan in scan_all_profiles():
        usage = profile_usage_info(scan.id)
        holder = busy_snapshot.get(scan.id)
        items.append(
            {
                "id": scan.id,
                "label": scan.label,
                "ready": scan.ready,
                "busy": holder is not None,
                "held_by": holder[0] if holder else None,
                "successes": usage["successes"],
                "discarded": usage["discarded"],
                "errors": scan.errors,
                "warnings": scan.warnings,
            }
        )
    return items


def startup_report() -> str:
    lines = [f"Perfiles en {PROFILES_DIR.resolve()}"]
    scans = scan_all_profiles()
    ready = sum(1 for s in scans if s.ready)
    lines.append(f"  Listos: {ready} / {len(scans)} (máx {MAX_SUCCESSES_PER_PROFILE} éxitos c/u)")
    discarded = sum(1 for s in scans if profile_usage_info(s.id)["discarded"])
    lines.append(f"  Descartados: {discarded}")
    for s in scans[:30]:
        if s.ready:
            u = profile_usage_info(s.id)
            lines.append(f"  ✅ {s.id} {s.label} ({u['successes']}/{MAX_SUCCESSES_PER_PROFILE})")
        else:
            err = "; ".join(s.errors[:2]) if s.errors else "?"
            lines.append(f"  ❌ {s.id} {s.label} — {err}")
    return "\n".join(lines)


def prepare_pool() -> None:
    """
    Compat con telegram_bot simplificado.
    Crea la carpeta del pool y registra estado en log.
    El pool se resuelve on-demand en acquire_next_profile / scan_all_profiles.
    """
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    log.info("%s", startup_report())


def maybe_purge_exhausted_profile(profile_id: str) -> bool:
    usage = profile_usage_info(profile_id)
    if usage["discarded"] or usage["successes"] >= MAX_SUCCESSES_PER_PROFILE:
        return remove_profile(profile_id)
    return False
