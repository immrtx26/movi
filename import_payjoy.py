"""Importa perfiles PayJoy aptos a movistar_perfiles listos para profile_prepare."""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROFILES_DIR = ROOT / "profiles" / "movistar_perfiles"
PAYJOY_BASE = Path(r"D:\projects\payjoy\user_photos\2068502930")
VALIDATION_JSON = PAYJOY_BASE / "validacion_profile_prepare.json"
CATALOG_PATH = ROOT / "profiles" / "profiles.json"


def clean_folder_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name.strip())[:80]


def next_profile_id(profiles_dir: Path) -> int:
    max_id = 0
    for p in profiles_dir.iterdir():
        if p.is_dir() and (m := re.match(r"^(\d+)_", p.name)):
            max_id = max(max_id, int(m.group(1)))
        elif p.is_dir() and p.name.isdigit():
            max_id = max(max_id, int(p.name))
    return max_id + 1


def import_profile(src: Path, dest: Path, label: str) -> list[str]:
    copied: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)

    mapping = [
        ("front.jpg", "front.jpg"),
        ("selfie.jpg", "selfie.jpg"),
        ("back.jpg", "back.jpg"),
        ("ocr.json", "ocr.json"),
    ]
    for src_name, dst_name in mapping:
        s = src / src_name
        if s.is_file():
            shutil.copy2(s, dest / dst_name)
            copied.append(dst_name)

    config = {"label": label}
    (dest / "config.json").write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    copied.append("config.json")
    return copied


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    data = json.loads(VALIDATION_JSON.read_text(encoding="utf-8"))
    aptos = data["aptos_list"]

    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    pid = next_profile_id(PROFILES_DIR)
    used_names: set[str] = set()
    catalog_entries: list[dict] = []
    imported = 0
    skipped = 0

    print(f"Importando {len(aptos)} perfiles PayJoy → {PROFILES_DIR}")
    print(f"ID inicial: {pid}")

    for folder_name in aptos:
        src = PAYJOY_BASE / folder_name
        ocr_path = src / "ocr.json"
        if not ocr_path.is_file():
            print(f"  SKIP {folder_name}: sin ocr.json")
            skipped += 1
            continue

        ocr = json.loads(ocr_path.read_text(encoding="utf-8"))
        label = ocr.get("nombre", folder_name).strip()
        base_name = clean_folder_name(label)
        folder_id = f"{pid}_{base_name}"

        while folder_id in used_names or (PROFILES_DIR / folder_id).exists():
            pid += 1
            folder_id = f"{pid}_{base_name}"

        dest = PROFILES_DIR / folder_id
        copied = import_profile(src, dest, label)
        used_names.add(folder_id)

        catalog_entries.append({
            "folder": folder_id,
            "id": folder_id,
            "label": label,
            "frente": str(dest / "front.jpg"),
            "selfie": str(dest / "selfie.jpg"),
            "ocr": str(dest / "ocr.json"),
            "prepare_ocr": False,
        })

        print(f"  [{pid:3d}] {label[:50]}")
        pid += 1
        imported += 1

    CATALOG_PATH.write_text(
        json.dumps({"profiles": catalog_entries}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"\nImportados: {imported} | Omitidos: {skipped}")
    print(f"Catálogo: {CATALOG_PATH} ({len(catalog_entries)} entradas)")


if __name__ == "__main__":
    main()
