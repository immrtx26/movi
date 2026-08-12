"""Prepara perfiles del catálogo (profiles.json) en movistar_perfiles."""
from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from profile_pool import (
    OCR_REQUIRED_KEYS,
    PROFILES_DIR,
    ROOT,
    _find_first,
    _folder_sort_key,
    _global_config,
    _label_from_folder,
    _named_sibling_folder,
    _is_numeric_pool_folder,
    scan_all_profiles,
    scan_profile_folder,
    startup_report,
)

log = logging.getLogger("profile-prepare")

CATALOG_PATH = ROOT / "profiles" / "profiles.json"

FRENTE_TARGET = "front.jpg"
SELFIE_TARGET = "selfie.jpg"
OCR_TARGET = "ocr.json"


@dataclass
class PrepareItem:
    folder_id: str
    label: str
    action: str
    ok: bool
    detail: str = ""


@dataclass
class PrepareReport:
    items: list[PrepareItem] = field(default_factory=list)

    def add(self, folder_id: str, label: str, action: str, ok: bool, detail: str = "") -> None:
        self.items.append(PrepareItem(folder_id, label, action, ok, detail))

    def summary(self) -> str:
        lines = ["Preparación de perfiles:"]
        for item in self.items:
            mark = "OK" if item.ok else "FAIL"
            suffix = f" — {item.detail}" if item.detail else ""
            lines.append(f"  [{mark}] {item.folder_id} ({item.label}) {item.action}{suffix}")
        lines.append("")
        lines.append(startup_report())
        return "\n".join(lines)


def _load_catalog() -> list[dict[str, Any]]:
    if not CATALOG_PATH.is_file():
        return []
    try:
        data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.warning("profiles.json inválido: %s", exc)
        return []
    profiles = data.get("profiles")
    if not isinstance(profiles, list):
        return []
    return [p for p in profiles if isinstance(p, dict)]


def _resolve_source(path_str: str) -> Path:
    p = Path(path_str)
    if p.is_absolute():
        return p
    return (ROOT / path_str).resolve()


def _folder_id(entry: dict[str, Any]) -> str:
    return str(entry.get("folder") or entry.get("id") or "").strip()


def _copy_if_needed(src: Path, dest: Path) -> bool:
    if not src.is_file():
        return False
    if dest.is_file() and dest.stat().st_size == src.stat().st_size:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return True


def _ocr_from_response(data: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    out = {
        "nombre": str(data.get("nombre", "")).strip(),
        "curp": str(data.get("curp", "")).strip(),
        "clave_elector": str(data.get("clave_elector", "")).strip(),
        "direccion": str(data.get("direccion", "")).strip(),
        "genero": str(data.get("genero", "M")).strip() or "M",
    }
    if all(out[k] for k in OCR_REQUIRED_KEYS):
        return out
    return None


def _write_ocr(folder: Path, ocr: dict[str, Any]) -> Path:
    path = folder / OCR_TARGET
    path.write_text(json.dumps(ocr, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _hubox_extract_ocr(
    front_path: Path,
    *,
    prep_phone: str,
    prep_otp: str,
) -> dict[str, Any]:
    from enroll_automation import _b64_or_text
    from enroll_replay import HuboxClient

    client = HuboxClient()
    frente_b64 = _b64_or_text(front_path)

    ini = client.inicio(prep_phone)
    tid = ini.get("track_id")
    if not tid:
        raise RuntimeError(f"Sin track_id: {json.dumps(ini, ensure_ascii=False)[:200]}")

    client.envia_otp(tid)
    val = client.valida_otp(tid, prep_otp)
    if not val.get("success"):
        raise RuntimeError("OTP de preparación inválido (HUBOX_PREP_PHONE / HUBOX_PREP_OTP)")

    client.detect_ine(frente_b64)
    det = client.detect_ine(frente_b64)
    crop = det.get("cropB64")
    if not crop:
        raise RuntimeError("detectINE no devolvió recorte")

    ocr_resp = client.ocr(tid, crop)
    if not ocr_resp.get("success"):
        raise RuntimeError(f"OCR Hubox falló: {json.dumps(ocr_resp, ensure_ascii=False)[:200]}")

    ocr = _ocr_from_response(ocr_resp)
    if ocr is None:
        raise RuntimeError("OCR incompleto en respuesta Hubox")
    return ocr


def _discover_folders() -> list[Path]:
    if not PROFILES_DIR.is_dir():
        return []
    return sorted(
        (p for p in PROFILES_DIR.iterdir() if p.is_dir()),
        key=_folder_sort_key,
    )


def _prepare_discovered_folder(folder: Path, global_cfg: dict[str, Any], report: PrepareReport) -> None:
    folder_id = folder.name
    local_cfg_path = folder / "config.json"
    local_cfg: dict[str, Any] = {}
    if local_cfg_path.is_file():
        try:
            local_cfg = json.loads(local_cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            local_cfg = {}

    label = _label_from_folder(folder_id, local_cfg, global_cfg)
    actions: list[str] = []

    if folder_id.isdigit():
        sibling = _named_sibling_folder(folder_id)
        if sibling is not None:
            for src_name in ("ocr.json", "ocr_data.json"):
                src = sibling / src_name
                if not src.is_file():
                    continue
                try:
                    if src_name == "ocr_data.json":
                        raw = json.loads(src.read_text(encoding="utf-8"))
                        ocr = _ocr_from_response(raw)
                        if ocr is None:
                            continue
                        _write_ocr(folder, ocr)
                    else:
                        shutil.copy2(src, folder / OCR_TARGET)
                    actions.append("ocr.json")
                    sibling_label = _label_from_folder(sibling.name, {}, global_cfg)
                    if sibling_label and local_cfg.get("label") != sibling_label:
                        local_cfg["label"] = sibling_label
                        local_cfg_path.write_text(
                            json.dumps(local_cfg, indent=2, ensure_ascii=False),
                            encoding="utf-8",
                        )
                        actions.append("config.json")
                    break
                except (OSError, json.JSONDecodeError) as exc:
                    report.add(folder_id, label, "sync", False, str(exc))

    ocr_data_path = folder / "ocr_data.json"
    ocr_path = folder / OCR_TARGET
    if ocr_data_path.is_file():
        try:
            raw = json.loads(ocr_data_path.read_text(encoding="utf-8"))
            ocr = _ocr_from_response(raw)
            if ocr is None:
                report.add(folder_id, label, "ocr", False, "ocr_data.json incompleto")
            elif not ocr_path.is_file() or json.loads(ocr_path.read_text(encoding="utf-8")) != ocr:
                _write_ocr(folder, ocr)
                actions.append("ocr.json")
        except (OSError, json.JSONDecodeError) as exc:
            report.add(folder_id, label, "ocr", False, str(exc))

    desired_label = _label_from_folder(folder_id, {}, global_cfg)
    if "_" in folder_id and local_cfg.get("label") != desired_label:
        local_cfg["label"] = desired_label
        local_cfg_path.write_text(json.dumps(local_cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        actions.append("config.json")

    if actions:
        report.add(folder_id, label, "prepare", True, ", ".join(actions))


def _sync_catalog_entry(entry: dict[str, Any], global_cfg: dict[str, Any], report: PrepareReport) -> None:
    folder_id = _folder_id(entry)
    if not folder_id:
        report.add("?", "?", "sync", False, "entrada sin folder/id")
        return

    label = str(entry.get("label") or global_cfg.get("labels", {}).get(folder_id) or f"Perfil {folder_id}")
    folder = PROFILES_DIR / folder_id
    folder.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []

    for src_key, dest_name in (("frente", FRENTE_TARGET), ("selfie", SELFIE_TARGET)):
        rel = entry.get(src_key)
        if not rel:
            continue
        src = _resolve_source(str(rel))
        if _copy_if_needed(src, folder / dest_name):
            copied.append(dest_name)

    ocr_ref = entry.get("ocr")
    if ocr_ref:
        src = _resolve_source(str(ocr_ref))
        if _copy_if_needed(src, folder / OCR_TARGET):
            copied.append(OCR_TARGET)
        elif not (folder / OCR_TARGET).is_file() and src.is_file():
            shutil.copy2(src, folder / OCR_TARGET)
            copied.append(OCR_TARGET)

    ocr_data = entry.get("ocr_data")
    if isinstance(ocr_data, dict) and _ocr_from_response(ocr_data):
        _write_ocr(folder, _ocr_from_response(ocr_data) or ocr_data)
        copied.append(OCR_TARGET)

    local_cfg_path = folder / "config.json"
    local_cfg: dict[str, Any] = {}
    if local_cfg_path.is_file():
        try:
            local_cfg = json.loads(local_cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            local_cfg = {}

    if label and local_cfg.get("label") != label:
        local_cfg["label"] = label
        local_cfg_path.write_text(json.dumps(local_cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        copied.append("config.json")

    if copied:
        report.add(folder_id, label, "sync", True, ", ".join(copied))
    else:
        report.add(folder_id, label, "sync", True, "sin cambios")


def _prepare_missing_ocr(
    scan,
    *,
    prep_phone: str | None,
    prep_otp: str,
    report: PrepareReport,
) -> None:
    ocr_path = scan.folder / OCR_TARGET
    if ocr_path.is_file():
        return

    frente = _find_first(
        scan.folder,
        (
            "front.jpg", "frente.jpg", "FRENTE.jpeg", "FRENTE.jpg", "FRENTE.png",
            "frente.b64", "ine_frente.b64",
        ),
    )
    if frente is None:
        report.add(scan.id, scan.label, "ocr", False, "sin front.jpg")
        return

    if not prep_phone:
        return

    try:
        ocr = _hubox_extract_ocr(
            frente,
            prep_phone=prep_phone,
            prep_otp=prep_otp,
        )
        _write_ocr(scan.folder, ocr)
        report.add(scan.id, scan.label, "ocr", True, ocr.get("nombre", "generado"))
    except Exception as exc:
        log.warning("OCR prep falló para %s: %s", scan.id, exc)
        report.add(scan.id, scan.label, "ocr", False, str(exc))


def prepare_all(
    *,
    hubox_user: str | None = None,
    hubox_password: str | None = None,
    prep_phone: str | None = None,
    prep_otp: str | None = None,
    auto_ocr: bool = True,
) -> PrepareReport:
    """Sincroniza catálogo y completa ocr.json faltantes."""
    report = PrepareReport()
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    global_cfg = _global_config()
    phone = (prep_phone or os.getenv("HUBOX_PREP_PHONE") or "").strip() or None
    otp = (prep_otp or os.getenv("HUBOX_PREP_OTP") or "0000").strip()

    for folder in _discover_folders():
        if not _is_numeric_pool_folder(folder):
            continue
        _prepare_discovered_folder(folder, global_cfg, report)

    for entry in _load_catalog():
        folder_id = _folder_id(entry)
        if not folder_id.isdigit():
            continue
        _sync_catalog_entry(entry, global_cfg, report)

    if auto_ocr:
        for scan in scan_all_profiles():
            if scan.id in {"—", "config"} or scan.ready:
                continue
            entry = next((e for e in _load_catalog() if _folder_id(e) == scan.id), {})
            if entry.get("prepare_ocr") is False:
                continue
            _prepare_missing_ocr(
                scan,
                prep_phone=phone,
                prep_otp=otp,
                report=report,
            )

    return report


def prepare_folder(folder_id: str, **kwargs: Any) -> PrepareReport:
    report = PrepareReport()
    global_cfg = _global_config()
    folder = PROFILES_DIR / folder_id
    if not folder.is_dir():
        report.add(folder_id, folder_id, "prepare", False, "carpeta no existe")
        return report

    entry = next((e for e in _load_catalog() if _folder_id(e) == folder_id), {"folder": folder_id})
    _sync_catalog_entry(entry, global_cfg, report)

    scan = scan_profile_folder(folder, global_cfg)
    phone = (kwargs.get("prep_phone") or os.getenv("HUBOX_PREP_PHONE") or "").strip() or None
    otp = (kwargs.get("prep_otp") or os.getenv("HUBOX_PREP_OTP") or "0000").strip()

    if not scan.ready:
        _prepare_missing_ocr(
            scan,
            prep_phone=phone,
            prep_otp=otp,
            report=report,
        )

    return report
