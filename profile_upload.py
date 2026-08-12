"""Alta de perfiles por usuario: validar frente/reverso/selfie y preparar far/close."""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from enroll_automation import _extract_qrs_from_reverso, _selfie_pair_480x640
from profile_pool import PROFILES_DIR, _validate_image

FAR_TARGET = "far.png"
CLOSE_TARGET = "close.png"
FAR_B64_TARGET = "selfie_far.b64"
CLOSE_B64_TARGET = "selfie_close.b64"


@dataclass
class UploadValidation:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    qr_count: int = 0
    face_detected: bool = False
    label: str = ""


@dataclass
class UploadResult:
    ok: bool
    profile_id: str = ""
    label: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    folder: Path | None = None


def normalize_to_jpeg(src: Path, dest: Path | None = None) -> Path:
    """Convierte cualquier imagen legible a JPEG en `dest` (o sobreescribe src)."""
    from PIL import Image

    out = dest or src.with_suffix(".jpg")
    with Image.open(src) as img:
        rgb = img.convert("RGB")
        out.parent.mkdir(parents=True, exist_ok=True)
        rgb.save(out, format="JPEG", quality=92, optimize=True)
    if dest is None and out.resolve() != src.resolve() and src.exists():
        try:
            src.unlink()
        except OSError:
            pass
    return out


def next_profile_id(profiles_dir: Path | None = None) -> int:
    base = profiles_dir or PROFILES_DIR
    base.mkdir(parents=True, exist_ok=True)
    max_id = 0
    for p in base.iterdir():
        if not p.is_dir():
            continue
        if p.name.isdigit():
            max_id = max(max_id, int(p.name))
        elif m := re.match(r"^(\d+)_", p.name):
            max_id = max(max_id, int(m.group(1)))
    return max_id + 1


def _detect_face(path: Path) -> bool:
    try:
        import cv2

        img = cv2.imread(str(path))
        if img is None:
            return False
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        faces = cascade.detectMultiScale(gray, 1.1, 5, minSize=(60, 60))
        return len(faces) > 0
    except Exception:
        return False


def _count_binary_qrs(back_path: Path) -> tuple[int, str | None]:
    """Devuelve (cantidad, error). OK GH = 2 QR binarios."""
    try:
        qr1, qr2 = _extract_qrs_from_reverso(back_path)
        return (2 if qr1 and qr2 else 0), None
    except Exception as exc:
        return 0, str(exc)


def prepare_far_close_files(selfie_path: Path, dest_dir: Path) -> tuple[Path, Path]:
    """Genera far/close 480x640 PNG + .b64 listos para biometría."""
    import base64

    dest_dir.mkdir(parents=True, exist_ok=True)
    far_b64, close_b64 = _selfie_pair_480x640(selfie_path)

    far_png = dest_dir / FAR_TARGET
    close_png = dest_dir / CLOSE_TARGET
    far_png.write_bytes(base64.b64decode(far_b64))
    close_png.write_bytes(base64.b64decode(close_b64))

    (dest_dir / FAR_B64_TARGET).write_text(far_b64, encoding="utf-8")
    (dest_dir / CLOSE_B64_TARGET).write_text(close_b64, encoding="utf-8")
    return far_png, close_png


def validate_upload_images(
    front_path: Path,
    back_path: Path,
    selfie_path: Path,
) -> UploadValidation:
    errors: list[str] = []
    warnings: list[str] = []

    errors.extend(_validate_image(front_path, "frente (anverso INE)"))
    errors.extend(_validate_image(back_path, "reverso INE"))
    errors.extend(_validate_image(selfie_path, "selfie"))

    qr_count = 0
    if not any("reverso" in e for e in errors):
        qr_count, qr_err = _count_binary_qrs(back_path)
        if qr_count < 2:
            errors.append(
                qr_err
                or "Reverso sin 2 QR binarios (INE antigua o foto ilegible). No apto para el pool."
            )

    face_ok = False
    if not any("selfie" in e for e in errors):
        face_ok = _detect_face(selfie_path)
        if not face_ok:
            errors.append("No se detectó un rostro en la selfie. Envía otra foto de cara frontal.")

    if errors:
        return UploadValidation(
            ok=False,
            errors=errors,
            warnings=warnings,
            qr_count=qr_count,
            face_detected=face_ok,
        )

    try:
        prepare_far_close_files(selfie_path, selfie_path.parent)
    except Exception as exc:
        return UploadValidation(
            ok=False,
            errors=[f"No se pudieron preparar far/close face: {exc}"],
            warnings=warnings,
            qr_count=qr_count,
            face_detected=face_ok,
        )

    return UploadValidation(
        ok=True,
        errors=[],
        warnings=warnings,
        qr_count=qr_count,
        face_detected=face_ok,
        label="Perfil usuario",
    )


def commit_validated_profile(
    front_path: Path,
    back_path: Path,
    selfie_path: Path,
    *,
    label: str | None = None,
    uploaded_by: int | None = None,
) -> UploadResult:
    """Valida y, solo si pasa, copia al pool como carpeta numérica."""
    staging = Path(tempfile.mkdtemp(prefix="profile_upload_"))
    try:
        front_dst = staging / "front.jpg"
        back_dst = staging / "back.jpg"
        selfie_dst = staging / "selfie.jpg"
        normalize_to_jpeg(front_path, front_dst)
        normalize_to_jpeg(back_path, back_dst)
        normalize_to_jpeg(selfie_path, selfie_dst)

        check = validate_upload_images(front_dst, back_dst, selfie_dst)
        if not check.ok:
            return UploadResult(ok=False, errors=check.errors, warnings=check.warnings)

        profile_id = str(next_profile_id())
        dest = PROFILES_DIR / profile_id
        if dest.exists():
            return UploadResult(ok=False, errors=[f"Carpeta destino ya existe: {profile_id}"])

        dest.mkdir(parents=True, exist_ok=False)
        for name in (
            "front.jpg",
            "back.jpg",
            "selfie.jpg",
            FAR_TARGET,
            CLOSE_TARGET,
            FAR_B64_TARGET,
            CLOSE_B64_TARGET,
        ):
            src = staging / name
            if src.is_file():
                shutil.copy2(src, dest / name)

        final_label = (label or check.label or f"Perfil {profile_id}").strip()
        config: dict[str, Any] = {"label": final_label, "source": "user_upload"}
        if uploaded_by is not None:
            config["uploaded_by"] = uploaded_by
        (dest / "config.json").write_text(
            json.dumps(config, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # Revalidar carpeta final (imágenes + QRs ya generados)
        recheck = validate_upload_images(dest / "front.jpg", dest / "back.jpg", dest / "selfie.jpg")
        if not recheck.ok:
            shutil.rmtree(dest, ignore_errors=True)
            return UploadResult(ok=False, errors=recheck.errors, warnings=recheck.warnings)

        return UploadResult(
            ok=True,
            profile_id=profile_id,
            label=final_label,
            warnings=check.warnings,
            folder=dest,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def cleanup_upload_dir(path: Path | None) -> None:
    if path is None:
        return
    try:
        if path.is_dir() and "_upload_tmp" in path.parts:
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass
