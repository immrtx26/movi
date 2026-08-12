"""Flujo Hubox automatizado usando un perfil preconfigurado (sin login)."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import requests

from enroll_replay import HuboxClient
from profile_pool import Profile

INE_API = "https://ine-services-2026.hubox.com/ine-services"

STATE_SM = {
    "AS": "01", "BC": "02", "BS": "03", "CC": "04", "CS": "05", "CH": "06",
    "CO": "07", "CL": "08", "DF": "09", "DG": "10", "GT": "11", "GR": "12",
    "HG": "13", "JC": "14", "MC": "15", "MN": "16", "MS": "17", "NT": "18",
    "NL": "19", "OC": "20", "PL": "21", "QT": "22", "QR": "23", "SP": "24",
    "SL": "25", "SR": "26", "TC": "27", "TS": "28", "TL": "29", "VZ": "30",
    "YN": "31", "ZS": "32",
}


class FlowError(Exception):
    """Error de negocio Hubox (OTP inválido, rechazo, etc.)."""

    def __init__(
        self,
        message: str,
        *,
        refundable: bool = False,
        retry_otp: bool = False,
        discard_profile: bool = False,
    ):
        super().__init__(message)
        self.refundable = refundable
        self.retry_otp = retry_otp
        self.discard_profile = discard_profile


class NetworkError(Exception):
    """Error de red/timeout — activación reembolsable."""

    pass


def _b64_file(path: Path) -> str:
    return __import__("base64").b64encode(path.read_bytes()).decode("ascii")


def _b64_or_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".b64":
        return path.read_text(encoding="utf-8").strip().replace("\n", "").replace("\r", "")
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        return _b64_file(path)
    try:
        text = path.read_text(encoding="utf-8").strip()
        if len(text) > 40 and all(c.isalnum() or c in "+/=\n\r" for c in text[:80]):
            return text.replace("\n", "").replace("\r", "")
    except (UnicodeDecodeError, OSError):
        pass
    return _b64_file(path)


def _selfie_b64_480x640(path: Path) -> str:
    """Compat: una sola selfie 480x640 (close). Preferir `_selfie_pair_480x640`."""
    _far, close = _selfie_pair_480x640(path)
    return close


def _cover_crop(img: "Image.Image", zoom: float = 1.0) -> "Image.Image":
    """Recorte centrado (o alrededor del rostro) a 480x640. zoom>1 = más cerca."""
    from PIL import Image

    target_w, target_h = 480, 640
    # Buscar rostro para anclar el crop
    cx, cy = img.width / 2, img.height / 2
    try:
        import cv2
        import numpy as np

        arr = np.array(img.convert("RGB"))
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        faces = cascade.detectMultiScale(gray, 1.1, 5, minSize=(60, 60))
        if len(faces):
            x, y, w, h = max(faces, key=lambda f: int(f[2]) * int(f[3]))
            cx, cy = x + w / 2, y + h / 2
    except Exception:
        pass

    # Ventana de captura relativa al tamaño de imagen; zoom acerca
    base = min(img.width / target_w, img.height / target_h)
    win_w = target_w * base / max(zoom, 0.5)
    win_h = target_h * base / max(zoom, 0.5)
    left = max(0, min(img.width - win_w, cx - win_w / 2))
    top = max(0, min(img.height - win_h, cy - win_h / 2))
    cropped = img.crop((int(left), int(top), int(left + win_w), int(top + win_h)))
    return cropped.resize((target_w, target_h), Image.Resampling.LANCZOS)


def _pil_to_png_b64(img: "Image.Image") -> str:
    import base64
    import io

    buf = io.BytesIO()
    img.convert("RGBA").save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _selfie_pair_480x640(path: Path) -> tuple[str, str]:
    """farFace + closeFace distintos a 480x640 PNG (como el HAR exitoso)."""
    import base64
    import io

    from PIL import Image

    raw_b64 = _b64_or_text(path)
    img = Image.open(io.BytesIO(base64.b64decode(raw_b64))).convert("RGB")
    far = _cover_crop(img, zoom=1.0)
    close = _cover_crop(img, zoom=1.45)
    return _pil_to_png_b64(far), _pil_to_png_b64(close)


def _extract_qrs_from_reverso(back_path: Path) -> tuple[str, str]:
    """Lee los 2 QR binarios del reverso INE (formato Hubox GH, ~858 bytes c/u)."""
    import cv2
    import zxingcpp

    img = cv2.imread(str(back_path))
    if img is None:
        raise FlowError(f"No se pudo leer reverso: {back_path.name}", refundable=False)

    payloads: list[bytes] = []
    for result in zxingcpp.read_barcodes(img):
        raw = getattr(result, "bytes", None)
        raw = bytes(raw) if raw is not None else result.text.encode("latin-1", errors="replace")
        text = result.text or ""
        if text.startswith("http"):
            continue
        if len(raw) < 200:
            continue
        payloads.append(raw)

    # Preferir pares con cabecera \x00\x00 / \x00\x01 (igual que HAR exitoso)
    indexed = [p for p in payloads if len(p) >= 2 and p[0] == 0 and p[1] in (0, 1)]
    if len(indexed) >= 2:
        indexed.sort(key=lambda p: p[1])
        qr1, qr2 = indexed[0], indexed[1]
    elif len(payloads) >= 2:
        payloads.sort(key=lambda p: (p[1] if len(p) > 1 else 99, len(p)))
        qr1, qr2 = payloads[0], payloads[1]
    else:
        raise FlowError(
            f"Reverso sin 2 QR binarios (encontrados: {len(payloads)}). Revisa back.jpg.",
            refundable=False,
        )

    return (
        __import__("base64").b64encode(qr1).decode("ascii"),
        __import__("base64").b64encode(qr2).decode("ascii"),
    )


def _resolve_qr_pair(profile: Profile, crop_b64: str, ocr: dict[str, Any]) -> tuple[str, str]:
    """GH: QRs del reverso. Fallback: genera-qrs (ine-services)."""
    if profile.back_path and profile.back_path.is_file():
        try:
            return _extract_qrs_from_reverso(profile.back_path)
        except FlowError:
            raise
        except Exception as exc:
            raise FlowError(f"Error leyendo QRs del reverso: {exc}", refundable=False) from exc

    biograficos = _build_biograficos(ocr)
    try:
        qr_r = requests.post(
            f"{INE_API}/genera-qrs",
            json={"biograficos": biograficos, "fotografia": crop_b64, "huellas": []},
            timeout=60,
        )
        qr_resp = qr_r.json()
    except requests.RequestException as exc:
        raise NetworkError(str(exc)) from exc

    if qr_resp.get("estatus") != 0:
        raise FlowError(f"genera-qrs: {json.dumps(qr_resp, ensure_ascii=False)[:300]}", refundable=False)

    return qr_resp["bytesQrs"][0], qr_resp["bytesQrs"][1]


def _build_biograficos(ocr: dict[str, Any]) -> str:
    a1 = str(ocr.get("apellido_paterno") or "").strip()
    a2 = str(ocr.get("apellido_materno") or "").strip()
    noms = str(ocr.get("nombre_pila") or "").strip()
    if not (a1 and noms):
        # Hubox OCR suele venir como APELLIDOS + NOMBRES
        parts = str(ocr.get("nombre") or "").split()
        a1 = parts[0] if len(parts) > 0 else ""
        a2 = parts[1] if len(parts) > 1 else ""
        noms = " ".join(parts[2:]) if len(parts) > 2 else ""
    curp = ocr["curp"]
    eid = STATE_SM.get(curp[11:13], "")
    vigencia = str(ocr.get("vigencia") or "2020-2030").strip() or "2020-2030"
    return "|".join([
        "I", vigencia, curp, "", "", ocr["clave_elector"], noms, a1, a2,
        ocr["direccion"], "", "", eid, "", "", ocr.get("genero", "M"),
        "", "", "", time.strftime("%Y%m%d"), "",
    ])


def _resolve_ocr_data(profile: Profile, ocr_resp: dict[str, Any]) -> dict[str, Any]:
    if profile.ocr_path and profile.ocr_path.exists():
        return json.loads(profile.ocr_path.read_text(encoding="utf-8"))
    needed = ("nombre", "curp", "clave_elector", "direccion")
    if all(k in ocr_resp for k in needed):
        return {
            "nombre": ocr_resp["nombre"],
            "curp": ocr_resp["curp"],
            "clave_elector": ocr_resp["clave_elector"],
            "direccion": ocr_resp["direccion"],
            "genero": ocr_resp.get("genero", "M"),
        }
    raise FlowError(
        f"Perfil `{profile.label}` sin ocr.json. Agrega el archivo al folder del perfil.",
        refundable=False,
    )



def start_and_send_otp(
    profile: Profile,
    phone: str,
    *,
    hubox_user: str | None = None,
    hubox_password: str | None = None,
) -> tuple[HuboxClient, str]:
    """Inicio + envío OTP (sin credenciales de portal). Devuelve cliente y track_id."""
    _ = profile, hubox_user, hubox_password
    client = HuboxClient()

    try:
        ini = client.inicio(phone)
    except requests.RequestException as exc:
        raise NetworkError(str(exc)) from exc
    except SystemExit as exc:
        raise FlowError(str(exc), refundable=False) from exc

    tid = ini.get("track_id")
    if not tid:
        raise FlowError(f"No se obtuvo track_id: {json.dumps(ini, ensure_ascii=False)[:300]}", refundable=False)

    try:
        send = client.envia_otp(tid)
    except requests.RequestException as exc:
        raise NetworkError(str(exc)) from exc
    except RuntimeError as exc:
        raise NetworkError(str(exc)) from exc

    if not send.get("success"):
        raise FlowError(f"OTP no enviado: {json.dumps(send, ensure_ascii=False)[:300]}", refundable=False)

    return client, tid


def complete_enroll(
    profile: Profile,
    client: HuboxClient | requests.Session,
    track_id: str,
    otp: str,
) -> dict[str, Any]:
    """Valida OTP y completa detectINE → biometría."""
    if isinstance(client, requests.Session):
        hubox = HuboxClient()
        hubox.session = client
    else:
        hubox = client

    otp = re.sub(r"\D", "", otp or "")[-4:]
    if len(otp) != 4:
        raise FlowError("OTP debe tener 4 dígitos.", refundable=False, retry_otp=True)

    frente_b64 = _b64_or_text(profile.frente_path)
    if (
        profile.far_path
        and profile.close_path
        and profile.far_path.is_file()
        and profile.close_path.is_file()
    ):
        far_b64 = _b64_or_text(profile.far_path)
        close_b64 = _b64_or_text(profile.close_path)
    else:
        far_b64, close_b64 = _selfie_pair_480x640(profile.selfie_path)

    try:
        val = hubox.valida_otp(track_id, otp)
    except requests.RequestException as exc:
        raise NetworkError(str(exc)) from exc
    except RuntimeError as exc:
        raise NetworkError(str(exc)) from exc

    if not val.get("success"):
        raise FlowError("Código OTP incorrecto o expirado.", refundable=False, retry_otp=True)

    try:
        hubox.detect_ine(frente_b64)
        det = hubox.detect_ine(frente_b64)
    except requests.RequestException as exc:
        raise NetworkError(str(exc)) from exc
    except RuntimeError as exc:
        raise NetworkError(str(exc)) from exc

    crop_b64 = det.get("cropB64")
    if not crop_b64:
        raise FlowError("Hubox no devolvió recorte de INE.", refundable=False)

    is_fake = float(det.get("isFake") or 0)
    if is_fake >= 25:
        raise FlowError(
            f"INE rechazada por Hubox (isFake={is_fake}). Usa otro perfil / foto de anverso.",
            refundable=False,
            discard_profile=True,
        )

    try:
        ocr_resp = hubox.ocr(track_id, crop_b64)
    except requests.RequestException as exc:
        raise NetworkError(str(exc)) from exc
    except RuntimeError as exc:
        raise NetworkError(str(exc)) from exc

    if not ocr_resp.get("success"):
        err = str(ocr_resp.get("error", ""))
        if err == "CURP_MAX_10" or ocr_resp.get("max10"):
            raise FlowError(
                "Este perfil ya alcanzó el límite de 10 vinculaciones en Hubox.",
                refundable=True,
                discard_profile=True,
            )
        raise FlowError(
            f"OCR falló: {ocr_resp.get('reason') or err or 'error desconocido'}",
            refundable=False,
        )

    ocr = _resolve_ocr_data(profile, ocr_resp)
    try:
        qr1, qr2 = _resolve_qr_pair(profile, crop_b64, ocr)
    except NetworkError:
        raise
    except FlowError:
        raise
    try:
        qrs = hubox.qrs(track_id, qr1, qr2)
    except requests.RequestException as exc:
        raise NetworkError(str(exc)) from exc
    except RuntimeError as exc:
        raise NetworkError(str(exc)) from exc

    if not qrs.get("success"):
        raise FlowError(f"QRs rechazados: {json.dumps(qrs, ensure_ascii=False)[:300]}", refundable=False)

    try:
        bio = hubox.biometric(track_id, far_b64, close_b64, retries=2)
    except requests.RequestException as exc:
        raise NetworkError(str(exc)) from exc
    except RuntimeError as exc:
        raise NetworkError(str(exc)) from exc

    if not bio.get("success"):
        err = str(bio.get("error") or "")
        if err == "RESET_STEP2_TIPO_INE_INVALID":
            raise FlowError(
                "Hubox invalidó el tipo de INE en biometría (RESET_STEP2_TIPO_INE_INVALID). "
                "Reintenta con otro perfil; far/close o el anverso pueden no coincidir.",
                refundable=False,
                discard_profile=True,
            )
        raise FlowError(f"Biometría falló: {json.dumps(bio, ensure_ascii=False)[:300]}", refundable=False)

    bio["track_id"] = bio.get("track_id") or track_id
    bio["profile_label"] = profile.label
    bio["ocr_nombre"] = ocr.get("nombre")
    return bio


def run_enroll_flow(
    profile: Profile,
    phone: str,
    otp: str,
    *,
    hubox_user: str | None = None,
    hubox_password: str | None = None,
) -> dict[str, Any]:
    """Flujo completo (útil para CLI/tests)."""
    client, tid = start_and_send_otp(profile, phone, hubox_user=hubox_user, hubox_password=hubox_password)
    return complete_enroll(profile, client, tid, otp)
