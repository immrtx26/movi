"""Flujo Hubox automatizado usando un perfil preconfigurado (sin login)."""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import requests

from enroll_replay import HuboxClient
from profile_pool import Profile

log = logging.getLogger("movistar-bot")

INE_API = "https://ine-services-2026.hubox.com/ine-services"

STATE_SM = {
    "AS": "01", "BC": "02", "BS": "03", "CC": "04", "CS": "05", "CH": "06",
    "CO": "07", "CL": "08", "DF": "09", "DG": "10", "GT": "11", "GR": "12",
    "HG": "13", "JC": "14", "MC": "15", "MN": "16", "MS": "17", "NT": "18",
    "NL": "19", "OC": "20", "PL": "21", "QT": "22", "QR": "23", "SP": "24",
    "SL": "25", "SR": "26", "TC": "27", "TS": "28", "TL": "29", "VZ": "30",
    "YN": "31", "ZS": "32",
}

YUNET_MODEL_CANDIDATES = (
    Path(__file__).resolve().parent / "models" / "face_detection_yunet_2023mar.onnx",
    Path(__file__).resolve().parent / "face_detection_yunet_2023mar.onnx",
)


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


# ---------------------------------------------------------------------------
# Detección de rostro: YuNet → Haar (ruta robusta) → centro de imagen
# ---------------------------------------------------------------------------

_yunet_detector = None
_yunet_tried = False
_haar_cascade = None
_haar_tried = False


def _get_yunet_detector():
    global _yunet_detector, _yunet_tried
    if _yunet_tried:
        return _yunet_detector
    _yunet_tried = True
    try:
        import cv2
        import os

        model_path = os.environ.get("YUNET_MODEL_PATH", "").strip()
        candidates = [Path(model_path)] if model_path else list(YUNET_MODEL_CANDIDATES)
        for p in candidates:
            if p and p.is_file():
                _yunet_detector = cv2.FaceDetectorYN.create(
                    str(p), "", (320, 320), score_threshold=0.5, nms_threshold=0.3
                )
                log.info("YuNet cargado desde %s", p)
                return _yunet_detector
    except Exception as exc:
        log.debug("YuNet no disponible: %s", exc)
        _yunet_detector = None
    return _yunet_detector


def _get_haar_cascade():
    """Busca haarcascade en varias rutas (headless a veces no tiene cv2.data usable)."""
    global _haar_cascade, _haar_tried
    if _haar_tried:
        return _haar_cascade
    _haar_tried = True
    try:
        import cv2

        candidates: list[str] = []
        try:
            candidates.append(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        except Exception:
            pass
        try:
            base = Path(cv2.__file__).resolve().parent
            candidates.extend(
                [
                    str(base / "data" / "haarcascade_frontalface_default.xml"),
                    str(base / "cv2" / "data" / "haarcascade_frontalface_default.xml"),
                    str(base / "data" / "haarcascade_frontalface_alt2.xml"),
                ]
            )
        except Exception:
            pass
        candidates.extend(
            [
                "/usr/local/lib/python3.11/site-packages/cv2/data/haarcascade_frontalface_default.xml",
                "/usr/lib/python3/dist-packages/cv2/data/haarcascade_frontalface_default.xml",
            ]
        )

        for path in candidates:
            if not path:
                continue
            if not Path(path).is_file():
                continue
            cascade = cv2.CascadeClassifier(path)
            if cascade is not None and not cascade.empty():
                _haar_cascade = cascade
                log.info("Haar cascade cargado: %s", path)
                return _haar_cascade
        log.warning("Haar cascade no encontrado en ninguna ruta conocida")
    except Exception as exc:
        log.warning("Error cargando Haar: %s", exc)
    _haar_cascade = None
    return None


def _detect_face_center(img_bgr) -> tuple[float, float] | None:
    """Devuelve (cx, cy) del rostro más grande, o None si no hay detector usable."""
    try:
        import cv2

        h, w = img_bgr.shape[:2]
        detector = _get_yunet_detector()
        if detector is not None:
            detector.setInputSize((w, h))
            _, faces = detector.detect(img_bgr)
            if faces is not None and len(faces) > 0:
                best = max(faces, key=lambda f: float(f[2]) * float(f[3]))
                return float(best[0] + best[2] / 2), float(best[1] + best[3] / 2)

        cascade = _get_haar_cascade()
        if cascade is not None:
            gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
            gray = cv2.equalizeHist(gray)
            for scale, neighbors, minsz in (
                (1.05, 4, 40),
                (1.1, 5, 50),
                (1.2, 3, 30),
            ):
                faces = cascade.detectMultiScale(
                    gray, scaleFactor=scale, minNeighbors=neighbors, minSize=(minsz, minsz)
                )
                if len(faces):
                    x, y, fw, fh = max(faces, key=lambda f: int(f[2]) * int(f[3]))
                    return float(x + fw / 2), float(y + fh / 2)
    except Exception as exc:
        log.debug("detect_face_center: %s", exc)
    return None


def face_detector_available() -> bool:
    """True si YuNet o Haar estan listos."""
    return _get_yunet_detector() is not None or _get_haar_cascade() is not None


def _cover_crop(img, zoom: float = 1.0):
    """Recorte centrado (o alrededor del rostro) a 480x640. zoom>1 = mas cerca."""
    from PIL import Image
    import numpy as np

    target_w, target_h = 480, 640
    cx, cy = img.width / 2, img.height / 2
    try:
        import cv2

        arr = np.array(img.convert("RGB"))
        bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        center = _detect_face_center(bgr)
        if center is not None:
            cx, cy = center
    except Exception:
        pass

    base = min(img.width / target_w, img.height / target_h)
    win_w = target_w * base / max(zoom, 0.5)
    win_h = target_h * base / max(zoom, 0.5)
    left = max(0, min(img.width - win_w, cx - win_w / 2))
    top = max(0, min(img.height - win_h, cy - win_h / 2))
    cropped = img.crop((int(left), int(top), int(left + win_w), int(top + win_h)))
    return cropped.resize((target_w, target_h), Image.Resampling.LANCZOS)


def _pil_to_png_b64(img) -> str:
    import base64
    import io

    buf = io.BytesIO()
    img.convert("RGBA").save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _selfie_pair_480x640(path: Path) -> tuple[str, str]:
    """farFace + closeFace distintos a 480x640 PNG."""
    import base64
    import io

    from PIL import Image

    raw_b64 = _b64_or_text(path)
    img = Image.open(io.BytesIO(base64.b64decode(raw_b64))).convert("RGB")
    far = _cover_crop(img, zoom=1.0)
    close = _cover_crop(img, zoom=1.45)
    return _pil_to_png_b64(far), _pil_to_png_b64(close)


# ---------------------------------------------------------------------------
# Lectura de QR del reverso (zxing + pyzbar, reintentos, 1 QR + MRZ)
# ---------------------------------------------------------------------------

def _qr_payloads_zxing(mat) -> list[bytes]:
    try:
        import zxingcpp
    except ImportError:
        return []

    payloads: list[bytes] = []
    try:
        results = zxingcpp.read_barcodes(mat)
    except Exception:
        return []
    for result in results:
        try:
            raw = getattr(result, "bytes", None)
            if raw is not None:
                raw = bytes(raw)
            else:
                raw = (result.text or "").encode("latin-1", errors="replace")
            text = (result.text or "").strip()
            if text.lower().startswith("http"):
                continue
            if len(raw) < 40 and len(text) < 40:
                continue
            payloads.append(raw)
        except Exception:
            continue
    return payloads


def _qr_payloads_pyzbar(mat) -> list[bytes]:
    try:
        from pyzbar import pyzbar
        from pyzbar.pyzbar import ZBarSymbol
    except ImportError:
        return []

    payloads: list[bytes] = []
    try:
        import cv2

        if len(mat.shape) == 3:
            gray = cv2.cvtColor(mat, cv2.COLOR_BGR2GRAY)
        else:
            gray = mat
        decoded = pyzbar.decode(gray, symbols=[ZBarSymbol.QRCODE])
        for obj in decoded:
            raw = obj.data or b""
            text = raw.decode("utf-8", errors="replace").strip()
            if text.lower().startswith("http"):
                continue
            if len(raw) < 40:
                continue
            payloads.append(bytes(raw))
    except Exception:
        pass
    return payloads


def _qr_payloads_from_mat(mat) -> list[bytes]:
    """Combina zxing-cpp + pyzbar y deduplica."""
    seen: set[bytes] = set()
    out: list[bytes] = []
    for p in _qr_payloads_zxing(mat) + _qr_payloads_pyzbar(mat):
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _build_qr_variants(img):
    """Variantes para maximizar lectura de QR (Telegram comprime bastante)."""
    import cv2

    variants = [img]
    h, w = img.shape[:2]

    for scale in (1.5, 2.0, 2.5, 3.0):
        variants.append(
            cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        )

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    variants.append(cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR))
    variants.append(cv2.cvtColor(cv2.equalizeHist(gray), cv2.COLOR_GRAY2BGR))

    for block, C in ((31, 5), (21, 10), (41, 3)):
        thr = cv2.adaptiveThreshold(
            enhanced, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block, C
        )
        variants.append(cv2.cvtColor(thr, cv2.COLOR_GRAY2BGR))

    variants.append(cv2.cvtColor(255 - enhanced, cv2.COLOR_GRAY2BGR))

    for angle in (-3, 3, -6, 6, -10, 10):
        M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
        variants.append(
            cv2.warpAffine(img, M, (w, h), borderMode=cv2.BORDER_REPLICATE)
        )

    return variants


def _extract_qrs_from_reverso(
    back_path: Path,
    *,
    allow_single: bool = True,
    max_variants: int = 16,
) -> tuple[str, str]:
    """
    Lee QR del reverso INE.
    - Ideal: 2 QR binarios (formato GH Hubox).
    - 1 QR + MRZ: acepta 1 si allow_single=True; el 2.o se completa con genera-qrs.
    """
    import cv2

    img = cv2.imread(str(back_path))
    if img is None:
        try:
            from PIL import Image
            import numpy as np

            pil = Image.open(back_path).convert("RGB")
            img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
        except Exception as exc:
            raise FlowError(f"No se pudo leer reverso: {back_path.name} ({exc})", refundable=False)

    best: list[bytes] = []
    for mat in _build_qr_variants(img)[: max(1, max_variants)]:
        payloads = _qr_payloads_from_mat(mat)
        if len(payloads) > len(best):
            best = payloads
        if len(best) >= 2:
            break

    payloads = best
    indexed = [p for p in payloads if len(p) >= 2 and p[0] == 0 and p[1] in (0, 1)]
    if len(indexed) >= 2:
        indexed.sort(key=lambda p: p[1])
        qr1, qr2 = indexed[0], indexed[1]
    elif len(payloads) >= 2:
        payloads_sorted = sorted(payloads, key=len, reverse=True)
        qr1, qr2 = payloads_sorted[0], payloads_sorted[1]
    elif len(payloads) == 1 and allow_single:
        qr1, qr2 = payloads[0], b""
    else:
        raise FlowError(
            f"Reverso sin QR legible (encontrados: {len(payloads)}). "
            "Usa foto nitida, sin reflejos, con los codigos bien visibles y de cerca.",
            refundable=False,
        )

    b64 = __import__("base64").b64encode
    return (
        b64(qr1).decode("ascii"),
        b64(qr2).decode("ascii") if qr2 else "",
    )


def count_qrs_in_reverso(back_path: Path) -> tuple[int, str | None]:
    """Util para validacion de upload. Devuelve (cantidad, error_opcional)."""
    try:
        qr1, qr2 = _extract_qrs_from_reverso(back_path, allow_single=True)
        n = 0
        if qr1:
            n += 1
        if qr2:
            n += 1
        return n, None
    except FlowError as exc:
        return 0, str(exc)
    except Exception as exc:
        return 0, str(exc)


def _resolve_qr_pair(profile: Profile, crop_b64: str, ocr: dict[str, Any]) -> tuple[str, str]:
    """GH: QRs del reverso. Si falta el 2.o o falla, fallback genera-qrs."""
    if profile.back_path and profile.back_path.is_file():
        try:
            qr1, qr2 = _extract_qrs_from_reverso(profile.back_path, allow_single=True)
            if qr1 and qr2:
                return qr1, qr2
            if qr1 and not qr2:
                log.info("Solo 1 QR en reverso; se completara con genera-qrs")
        except FlowError as exc:
            log.warning("QR reverso fallo, fallback genera-qrs: %s", exc)
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
        raise FlowError(
            f"genera-qrs: {json.dumps(qr_resp, ensure_ascii=False)[:300]}",
            refundable=False,
        )

    return qr_resp["bytesQrs"][0], qr_resp["bytesQrs"][1]


def _build_biograficos(ocr: dict[str, Any]) -> str:
    a1 = str(ocr.get("apellido_paterno") or "").strip()
    a2 = str(ocr.get("apellido_materno") or "").strip()
    noms = str(ocr.get("nombre_pila") or "").strip()
    if not (a1 and noms):
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


def _flatten_ocr_dict(ocr_resp: dict[str, Any]) -> dict[str, Any]:
    """Une niveles anidados típicos de Hubox (data/ocr/result)."""
    merged: dict[str, Any] = {}
    stack: list[Any] = [ocr_resp]
    seen: set[int] = set()
    while stack:
        cur = stack.pop()
        if not isinstance(cur, dict):
            continue
        i = id(cur)
        if i in seen:
            continue
        seen.add(i)
        for k, v in cur.items():
            if isinstance(v, dict) and k.lower() in {
                "data", "ocr", "result", "payload", "response", "persona", "ine"
            }:
                stack.append(v)
            elif k not in merged and v is not None and v != "":
                merged[k] = v
            elif isinstance(v, dict):
                stack.append(v)
    return merged


def _pick_ocr_field(merged: dict[str, Any], *names: str) -> str:
    lower_map = {str(k).lower(): v for k, v in merged.items()}
    for name in names:
        for key in (name, name.lower(), name.replace("_", ""), name.replace("_", "").lower()):
            if key in merged and str(merged[key]).strip():
                return str(merged[key]).strip()
            if key.lower() in lower_map and str(lower_map[key.lower()]).strip():
                return str(lower_map[key.lower()]).strip()
    return ""


def _resolve_ocr_data(profile: Profile, ocr_resp: dict[str, Any]) -> dict[str, Any]:
    """OCR local (ocr.json) o campos devueltos por Hubox enroll_ocr."""
    if profile.ocr_path and profile.ocr_path.exists():
        try:
            data = json.loads(profile.ocr_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("curp"):
                return data
        except (OSError, json.JSONDecodeError):
            pass

    merged = _flatten_ocr_dict(ocr_resp if isinstance(ocr_resp, dict) else {})
    nombre = _pick_ocr_field(
        merged, "nombre", "nombreCompleto", "nombre_completo", "name", "fullName"
    )
    curp = _pick_ocr_field(merged, "curp", "CURP")
    clave = _pick_ocr_field(
        merged,
        "clave_elector",
        "claveElector",
        "clave_de_elector",
        "claveDeElector",
        "electorKey",
    )
    direccion = _pick_ocr_field(
        merged, "direccion", "domicilio", "address", "direccionCompleta", "calle"
    )
    genero = _pick_ocr_field(merged, "genero", "sexo", "gender") or "M"
    if genero.upper() in ("H", "HOMBRE", "MASCULINO", "MALE"):
        genero = "M"
    elif genero.upper() in ("M", "MUJER", "FEMENINO", "FEMALE", "F"):
        # Hubox a veces usa M para mujer; biograficos espera M/F o M
        genero = "M" if genero.upper() in ("M",) and "mujer" not in genero.lower() else genero
        if genero.upper() in ("MUJER", "FEMENINO", "FEMALE", "F"):
            genero = "F"

    vigencia = _pick_ocr_field(merged, "vigencia", "vigenciaINE", "validity")
    apellido_paterno = _pick_ocr_field(merged, "apellido_paterno", "apellidoPaterno", "primerApellido")
    apellido_materno = _pick_ocr_field(merged, "apellido_materno", "apellidoMaterno", "segundoApellido")
    nombre_pila = _pick_ocr_field(merged, "nombre_pila", "nombres", "primerNombre")

    if curp and clave and (nombre or (apellido_paterno and nombre_pila)) and direccion:
        if not nombre:
            nombre = " ".join(x for x in (apellido_paterno, apellido_materno, nombre_pila) if x).strip()
        out = {
            "nombre": nombre,
            "curp": curp.upper(),
            "clave_elector": clave.upper(),
            "direccion": direccion,
            "genero": genero[:1].upper() if genero else "M",
        }
        if vigencia:
            out["vigencia"] = vigencia
        if apellido_paterno:
            out["apellido_paterno"] = apellido_paterno
        if apellido_materno:
            out["apellido_materno"] = apellido_materno
        if nombre_pila:
            out["nombre_pila"] = nombre_pila
        return out

    # Guardar pista de keys recibidas para depurar
    keys_hint = ", ".join(sorted(str(k) for k in merged.keys())[:25]) or "(vacío)"
    missing = []
    if not curp:
        missing.append("curp")
    if not clave:
        missing.append("clave_elector")
    if not nombre and not (apellido_paterno and nombre_pila):
        missing.append("nombre")
    if not direccion:
        missing.append("direccion")
    raise FlowError(
        f"OCR incompleto en perfil  (faltan: {', '.join(missing)}). "
        f"Keys Hubox: {keys_hint}. "
        "Reintenta con foto de anverso más nítida o agrega ocr.json al perfil.",
        refundable=True,
    )


def start_and_send_otp(
    profile: Profile,
    phone: str,
    *,
    hubox_user: str | None = None,
    hubox_password: str | None = None,
) -> tuple[HuboxClient, str]:
    """Inicio + envio OTP. Devuelve cliente y track_id."""
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
        raise FlowError(
            f"No se obtuvo track_id: {json.dumps(ini, ensure_ascii=False)[:300]}",
            refundable=False,
        )

    try:
        send = client.envia_otp(tid)
    except requests.RequestException as exc:
        raise NetworkError(str(exc)) from exc
    except RuntimeError as exc:
        raise NetworkError(str(exc)) from exc

    if not send.get("success"):
        raise FlowError(
            f"OTP no enviado: {json.dumps(send, ensure_ascii=False)[:300]}",
            refundable=False,
        )

    return client, tid


def complete_enroll(
    profile: Profile,
    client: HuboxClient | requests.Session,
    track_id: str,
    otp: str,
) -> dict[str, Any]:
    """Valida OTP y completa detectINE -> biometria."""
    if isinstance(client, requests.Session):
        hubox = HuboxClient()
        hubox.session = client
    else:
        hubox = client

    otp = re.sub(r"\D", "", otp or "")[-4:]
    if len(otp) != 4:
        raise FlowError("OTP debe tener 4 digitos.", refundable=False, retry_otp=True)

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
        raise FlowError("Codigo OTP incorrecto o expirado.", refundable=False, retry_otp=True)

    try:
        hubox.detect_ine(frente_b64)
        det = hubox.detect_ine(frente_b64)
    except requests.RequestException as exc:
        raise NetworkError(str(exc)) from exc
    except RuntimeError as exc:
        raise NetworkError(str(exc)) from exc

    crop_b64 = det.get("cropB64")
    if not crop_b64:
        raise FlowError("Hubox no devolvio recorte de INE.", refundable=False)

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
                "Este perfil ya alcanzo el limite de 10 vinculaciones en Hubox.",
                refundable=True,
                discard_profile=True,
            )
        raise FlowError(
            f"OCR fallo: {ocr_resp.get('reason') or err or 'error desconocido'}",
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
        raise FlowError(
            f"QRs rechazados: {json.dumps(qrs, ensure_ascii=False)[:300]}",
            refundable=False,
        )

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
                "Hubox invalido el tipo de INE en biometria (RESET_STEP2_TIPO_INE_INVALID). "
                "Reintenta con otro perfil; far/close o el anverso pueden no coincidir.",
                refundable=False,
                discard_profile=True,
            )
        raise FlowError(
            f"Biometria fallo: {json.dumps(bio, ensure_ascii=False)[:300]}",
            refundable=False,
        )

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
    """Flujo completo (util para CLI/tests)."""
    client, tid = start_and_send_otp(
        profile, phone, hubox_user=hubox_user, hubox_password=hubox_password
    )
    return complete_enroll(profile, client, tid, otp)
