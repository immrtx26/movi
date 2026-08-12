#!/usr/bin/env python3
"""
Flujo presencial Movistar Hubox — login con credenciales + vinculación (sin OTP SMS).

Cronología:
  LOGIN (HAR movistar_acceso.har)
    1. GET  /portabilidad
    2. POST /api/auth/enroll-login (pre-CF, opcional)
    3. Cloudflare cf_clearance (manual)
    4. GET  /login?redirect=%2Fportabilidad
    5. POST /api/auth/enroll-login → __Host-hubox_session
    6. Verificar sesión

  ENROLL (sesión portal — no usa enroll_envia_otp ni enroll_valida_otp)
    7.  POST /api/auth/enroll-inicio (proxy portal, RSA)
    8.  POST api-v1 /enroll_detectINE (×2)
    9.  POST api-v1 /enroll_ocr
    10. POST ine-services /genera-qrs
    11. POST api-v1 /enroll_qrs
    12. POST api-v1 /enroll_biometric

Uso:
  set CF_CLEARANCE=...
  set HUBOX_USER=...
  set HUBOX_PASSWORD=...
  python movistar_presencial.py full --phone 5512345678 --frente front.jpg --selfie selfie.jpg --ocr ocr.json

  python movistar_presencial.py login
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

load_dotenv()

# --- Constantes ---
FRONTEND = "https://registro-telefonica-movistar.hubox.com"
API_BASE = "https://api-v1.hubox.com"
INE_API = "https://ine-services-2026.hubox.com/ine-services"
LOGIN_URL = f"{FRONTEND}/api/auth/enroll-login"
ENROLL_INICIO_URL = f"{FRONTEND}/api/auth/enroll-inicio"
ACCESS_KEY = os.getenv("HUBOX_ACCESS_KEY", "ak_TelefonicaPruebas")
AMBIENTE = os.getenv("HUBOX_AMBIENTE", "movistar_prod")
FLUJO = os.getenv("HUBOX_FLUJO", "movistar_registro")
STATE_PATH = Path(".hubox_presencial_state.json")

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) SamsungBrowser/30.0 Chrome/143.0.0.0 Mobile Safari/537.36"
)

PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAsoEUrr8XEqIRnVLtKyID
14X05OM4cBsnStN+QS6OK3X8ygB49Nt+plYLgZ18vBOq4pic7h6wK315QoqEVlWK
ixKTl42SPek0lrGXo9FT8eQzdM6fm/vJgFg/nDcXtULXnl++/wAVXvTICXDk/8bE
1IF40mcDIMQqF1+AkDUD+PV8Be0/F25Qe951N1RlWazQFopP7ARCKBQi+30g9mZJ
ftZ9w9OpMWlIFo9sOdbW3N+Em7apFHYjJzBuWgsVPEs/UahoTDu8v3laO8FDImo3
mKURWsaVMfxY7hXVskeHQ4E00KYSPVbppgCK46R3miwuMD/gHz/K8OluSbgWgb97
XwIDAQAB
-----END PUBLIC KEY-----"""

SESSION_COOKIE = "__Host-hubox_session"

STATE_SM = {
    "AS": "01", "BC": "02", "BS": "03", "CC": "04", "CS": "05", "CH": "06",
    "CO": "07", "CL": "08", "DF": "09", "DG": "10", "GT": "11", "GR": "12",
    "HG": "13", "JC": "14", "MC": "15", "MN": "16", "MS": "17", "NT": "18",
    "NL": "19", "OC": "20", "PL": "21", "QT": "22", "QR": "23", "SP": "24",
    "SL": "25", "SR": "26", "TC": "27", "TS": "28", "TL": "29", "VZ": "30",
    "YN": "31", "ZS": "32",
}


@dataclass
class PresencialProfile:
    frente: Path
    selfie: Path
    ocr_path: Path | None = None
    back: Path | None = None
    label: str = ""


def _log(step: int | str, msg: str, detail: str = "") -> None:
    suffix = f" — {detail}" if detail else ""
    print(f"[{step}] {msg}{suffix}")


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(patch: dict[str, Any]) -> dict[str, Any]:
    st = load_state()
    st.update(patch)
    st["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    STATE_PATH.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    return st


def normalize_phone(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 12 and digits.startswith("52"):
        digits = digits[2:]
    if len(digits) != 10:
        raise ValueError(f"Teléfono inválido (10 dígitos MX): {phone!r}")
    return digits


def encrypt_login_payload(payload: dict[str, Any]) -> str:
    """Login portal: Base64(JSON) → RSA-OAEP → Base64."""
    pub = serialization.load_pem_public_key(PUBLIC_KEY_PEM.encode())
    json_b64 = base64.b64encode(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    ).decode()
    ciphertext = pub.encrypt(
        json_b64.encode(),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(ciphertext).decode()


def encrypt_start_payload(payload: dict[str, Any]) -> str:
    """Enroll inicio: UTF-8(JSON) → RSA-OAEP → Base64 (igual enroll_replay)."""
    pub = serialization.load_pem_public_key(PUBLIC_KEY_PEM.encode())
    plaintext = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ciphertext = pub.encrypt(
        plaintext,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(ciphertext).decode()


def build_session(cf_clearance: str | None = None) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "es-US,es-419;q=0.9,es;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "sec-ch-ua": '"Samsung Internet";v="30.0", "Chromium";v="143", "Not A(Brand";v="24"',
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": '"Android"',
        }
    )
    if cf_clearance:
        s.cookies.set("cf_clearance", cf_clearance.strip(), domain="registro-telefonica-movistar.hubox.com")
    return s


def _api_headers(referer: str | None = None) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": FRONTEND,
        "Referer": referer or f"{FRONTEND}/portabilidad",
    }


def _post_json(
    session: requests.Session,
    url: str,
    body: dict[str, Any],
    *,
    timeout: float = 120.0,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    headers = _api_headers()
    if extra_headers:
        headers.update(extra_headers)
    preview = {
        k: (f"<{len(v)} chars>" if isinstance(v, str) and len(v) > 120 else v)
        for k, v in body.items()
    }
    _log("→", f"POST {url}", json.dumps(preview, ensure_ascii=False)[:200])
    resp = session.post(url, json=body, headers=headers, timeout=timeout)
    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
    shown = {
        k: (v[:60] + f"...<{len(v)} chars>" if isinstance(v, str) and len(v) > 200 else v)
        for k, v in data.items()
    }
    _log("←", f"{resp.status_code}", json.dumps(shown, ensure_ascii=False)[:300])
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {json.dumps(shown, ensure_ascii=False)[:500]}")
    return data


def file_to_b64(path: str | Path) -> str:
    p = Path(path)
    data = p.read_bytes()
    try:
        text = data.decode("utf-8").strip()
        if text and all(c.isalnum() or c in "+/=\n\r" for c in text[:200]) and len(text) > 40:
            base64.b64decode(text, validate=False)
            return text.replace("\n", "").replace("\r", "")
    except Exception:
        pass
    return base64.b64encode(data).decode("ascii")


def selfie_to_b64_480x640(path: str | Path) -> str:
    """Hubox enroll_biometric solo acepta 480x640 / 640x480."""
    from enroll_automation import _selfie_b64_480x640

    return _selfie_b64_480x640(Path(path))


def selfie_pair_b64_480x640(path: str | Path) -> tuple[str, str]:
    from enroll_automation import _selfie_pair_480x640

    return _selfie_pair_480x640(Path(path))


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


def _resolve_ocr_data(profile: PresencialProfile, ocr_resp: dict[str, Any]) -> dict[str, Any]:
    if profile.ocr_path and profile.ocr_path.is_file():
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
    raise RuntimeError("Falta ocr.json local o respuesta OCR incompleta.")


# ---------------------------------------------------------------------------
# LOGIN (pasos 1–6)
# ---------------------------------------------------------------------------

def paso_1_cargar_portabilidad(session: requests.Session) -> None:
    _log(1, "GET /portabilidad")
    resp = session.get(
        f"{FRONTEND}/portabilidad",
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
            "Upgrade-Insecure-Requests": "1",
        },
        timeout=30,
    )
    resp.raise_for_status()
    _log(1, "OK", f"HTTP {resp.status_code}")


def paso_2_intento_login_precf(session: requests.Session, user: str, password: str) -> dict[str, Any]:
    _log(2, "POST /api/auth/enroll-login (pre-CF)")
    payload = {
        "action": "login",
        "user": user.strip(),
        "password": password,
        "ambiente": AMBIENTE,
        "tipo": 2,
        "access_key": ACCESS_KEY,
    }
    data = _post_json(
        session,
        LOGIN_URL,
        {"data": encrypt_login_payload(payload)},
        timeout=30,
        extra_headers={"Referer": f"{FRONTEND}/portabilidad"},
    )
    return data


def paso_3_cloudflare_nota(cf_clearance: str | None) -> None:
    _log(3, "Cloudflare JSD challenge")
    if cf_clearance:
        _log(3, "OK", "cf_clearance provisto")
        return
    _log(3, "SKIP", "Pasa CF_CLEARANCE desde Reqable/navegador")


def paso_4_cargar_login(session: requests.Session) -> None:
    _log(4, "GET /login?redirect=%2Fportabilidad")
    resp = session.get(
        f"{FRONTEND}/login?redirect=%2Fportabilidad",
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
            "Referer": f"{FRONTEND}/portabilidad",
        },
        timeout=30,
    )
    resp.raise_for_status()
    _log(4, "OK", f"HTTP {resp.status_code}")


def paso_5_enroll_login(session: requests.Session, user: str, password: str) -> dict[str, Any]:
    _log(5, "POST /api/auth/enroll-login (inicio sesión)")
    payload = {
        "action": "login",
        "user": user.strip(),
        "password": password,
        "ambiente": AMBIENTE,
        "tipo": 2,
        "access_key": ACCESS_KEY,
    }
    return _post_json(
        session,
        LOGIN_URL,
        {"data": encrypt_login_payload(payload)},
        timeout=30,
        extra_headers={"Referer": f"{FRONTEND}/login?redirect=%2Fportabilidad"},
    )


def paso_6_verificar_sesion(session: requests.Session) -> str:
    token = session.cookies.get(SESSION_COOKIE)
    if not token:
        raise RuntimeError(f"No se recibió {SESSION_COOKIE}. Revisa credenciales y cf_clearance.")
    _log(6, "Sesión iniciada", f"{SESSION_COOKIE}={token[:48]}…")
    return token


def login_presencial(
    user: str,
    password: str,
    *,
    cf_clearance: str | None = None,
    skip_precf: bool = False,
) -> requests.Session:
    session = build_session(cf_clearance)
    paso_1_cargar_portabilidad(session)
    if not skip_precf:
        paso_2_intento_login_precf(session, user, password)
    paso_3_cloudflare_nota(cf_clearance)
    if not cf_clearance:
        raise RuntimeError("Falta CF_CLEARANCE para login presencial.")
    paso_4_cargar_login(session)
    data = paso_5_enroll_login(session, user, password)
    if not data.get("success"):
        err = data.get("message") or data.get("error") or json.dumps(data)
        raise RuntimeError(f"Login falló: {err}")
    paso_6_verificar_sesion(session)
    return session


# ---------------------------------------------------------------------------
# ENROLL presencial sin OTP (pasos 7–12)
# ---------------------------------------------------------------------------

def paso_7_enroll_inicio(session: requests.Session, phone: str, tipo_doc: str = "ine") -> str:
    _log(7, "POST /api/auth/enroll-inicio (proxy portal)")
    plain = {
        "numero": normalize_phone(phone),
        "flujo": FLUJO,
        "tipo_doc": tipo_doc,
        "access_key": ACCESS_KEY,
        "ambiente": AMBIENTE,
    }
    _log(7, "payload", json.dumps(plain, ensure_ascii=False))
    data = _post_json(
        session,
        ENROLL_INICIO_URL,
        {"data": encrypt_start_payload(plain)},
        timeout=30,
        extra_headers={"Referer": f"{FRONTEND}/portabilidad"},
    )
    tid = data.get("track_id")
    if not tid:
        raise RuntimeError(f"Sin track_id: {json.dumps(data, ensure_ascii=False)[:300]}")
    save_state({"track_id": tid, "phone": plain["numero"], "flujo": FLUJO, "inicio": data})
    _log(7, "OK", f"track_id={tid}")
    return tid


def paso_8_detect_ine(session: requests.Session, frente_b64: str) -> dict[str, Any]:
    _log(8, "POST /enroll_detectINE (1ra)")
    _post_json(session, f"{API_BASE}/enroll_detectINE", {"img": frente_b64, "access_key": ACCESS_KEY}, timeout=180)
    _log(8, "POST /enroll_detectINE (2da)")
    data = _post_json(
        session,
        f"{API_BASE}/enroll_detectINE",
        {"img": frente_b64, "access_key": ACCESS_KEY},
        timeout=180,
    )
    crop = data.get("cropB64")
    if not crop:
        raise RuntimeError("Hubox no devolvió recorte de INE.")
    crop_path = Path("last_crop_presencial.jpg")
    crop_path.write_bytes(base64.b64decode(crop))
    _log(8, "OK", f"crop → {crop_path}")
    save_state({"crop_path": str(crop_path), "detect_ine": {k: data[k] for k in data if k != "cropB64"}})
    return data


def paso_9_ocr(session: requests.Session, track_id: str, crop_b64: str) -> dict[str, Any]:
    _log(9, "POST /enroll_ocr")
    data = _post_json(
        session,
        f"{API_BASE}/enroll_ocr",
        {"img": crop_b64, "track_id": track_id, "access_key": ACCESS_KEY},
        timeout=180,
    )
    if not data.get("success"):
        err = str(data.get("error", ""))
        if err == "CURP_MAX_10" or data.get("max10"):
            raise RuntimeError("Perfil alcanzó límite de 10 vinculaciones (CURP_MAX_10).")
        raise RuntimeError(f"OCR falló: {data.get('reason') or err or 'error desconocido'}")
    save_state({"track_id": track_id, "ocr": data})
    _log(9, "OK")
    return data


def paso_10_genera_qrs(
    crop_b64: str,
    ocr: dict[str, Any],
    back_path: Path | None = None,
) -> tuple[str, str]:
    if back_path and back_path.is_file():
        _log(10, "QRs desde reverso INE", str(back_path))
        from enroll_automation import _extract_qrs_from_reverso

        qr1, qr2 = _extract_qrs_from_reverso(back_path)
        _log(10, "OK", f"QR1={len(qr1)}b QR2={len(qr2)}b (reverso)")
        return qr1, qr2

    _log(10, "POST ine-services/genera-qrs")
    biograficos = _build_biograficos(ocr)
    resp = requests.post(
        f"{INE_API}/genera-qrs",
        json={"biograficos": biograficos, "fotografia": crop_b64, "huellas": []},
        timeout=60,
    )
    data = resp.json()
    if data.get("estatus") != 0:
        raise RuntimeError(f"genera-qrs: {json.dumps(data, ensure_ascii=False)[:300]}")
    qr1, qr2 = data["bytesQrs"][0], data["bytesQrs"][1]
    _log(10, "OK", f"QR1={len(qr1)}b QR2={len(qr2)}b")
    return qr1, qr2


def paso_11_enroll_qrs(session: requests.Session, track_id: str, qr1: str, qr2: str) -> dict[str, Any]:
    _log(11, "POST /enroll_qrs")
    data = _post_json(
        session,
        f"{API_BASE}/enroll_qrs",
        {"qr_b64_1": qr1, "qr_b64_2": qr2, "track_id": track_id, "access_key": ACCESS_KEY},
        timeout=180,
    )
    if not data.get("success"):
        raise RuntimeError(f"QRs rechazados: {json.dumps(data, ensure_ascii=False)[:300]}")
    save_state({"track_id": track_id, "qrs": data})
    _log(11, "OK")
    return data


def paso_12_biometric(
    session: requests.Session,
    track_id: str,
    far_b64: str,
    close_b64: str | None = None,
    devices: str = "camera 1, facing front 480x640",
) -> dict[str, Any]:
    _log(12, "POST /enroll_biometric")
    close = close_b64 or far_b64
    body = {
        "farFace": far_b64,
        "closeFace": close,
        "devices": devices,
        "track_id": track_id,
        "access_key": ACCESS_KEY,
    }
    last: dict[str, Any] = {}
    for attempt in range(1, 3):
        try:
            last = _post_json(session, f"{API_BASE}/enroll_biometric", body, timeout=300)
            if not last.get("success"):
                raise RuntimeError(json.dumps(last, ensure_ascii=False)[:300])
            save_state({"track_id": track_id, "biometric": last})
            _log(12, "OK", f"similarity={last.get('similarity')} prediction={last.get('prediction')}")
            return last
        except RuntimeError as exc:
            _log(12, f"intento {attempt}/2 falló", str(exc))
            if attempt >= 2:
                raise
            time.sleep(5)
    return last


def flujo_presencial_completo(
    user: str,
    password: str,
    phone: str,
    profile: PresencialProfile,
    *,
    cf_clearance: str | None = None,
    skip_precf: bool = False,
    skip_biometric: bool = False,
    devices: str = "camera 1, facing front 480x640",
) -> dict[str, Any]:
    """Login portal + enroll completo. Sin OTP SMS — la sesión portal autentica al operador."""
    session = login_presencial(user, password, cf_clearance=cf_clearance, skip_precf=skip_precf)

    track_id = paso_7_enroll_inicio(session, phone)

    frente_b64 = file_to_b64(profile.frente)
    far_b64, close_b64 = selfie_pair_b64_480x640(profile.selfie)

    det = paso_8_detect_ine(session, frente_b64)
    crop_b64 = det["cropB64"]

    ocr_resp = paso_9_ocr(session, track_id, crop_b64)
    ocr = _resolve_ocr_data(profile, ocr_resp)

    qr1, qr2 = paso_10_genera_qrs(crop_b64, ocr, back_path=profile.back)
    paso_11_enroll_qrs(session, track_id, qr1, qr2)

    if skip_biometric:
        _log(12, "SKIP biometría")
        return {"track_id": track_id, "skipped_biometric": True}

    bio = paso_12_biometric(session, track_id, far_b64, close_b64, devices=devices)
    bio["track_id"] = bio.get("track_id") or track_id
    bio["ocr_nombre"] = ocr.get("nombre")
    bio["profile_label"] = profile.label or profile.frente.parent.name
    return bio


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _profile_from_args(args: argparse.Namespace) -> PresencialProfile:
    if not args.frente or not args.selfie:
        raise SystemExit("full requiere --frente y --selfie")
    ocr = Path(args.ocr) if args.ocr else None
    back = Path(args.back) if getattr(args, "back", None) else None
    return PresencialProfile(
        frente=Path(args.frente),
        selfie=Path(args.selfie),
        ocr_path=ocr,
        back=back,
        label=args.label or "",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Flujo presencial Movistar Hubox (credenciales, sin OTP SMS)")
    sub = parser.add_subparsers(dest="cmd")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--user", default=os.getenv("HUBOX_USER", ""))
    common.add_argument("--password", default=os.getenv("HUBOX_PASSWORD", ""))
    common.add_argument("--cf-clearance", default=os.getenv("CF_CLEARANCE", ""))
    common.add_argument("--skip-precf", action="store_true")

    p_login = sub.add_parser("login", parents=[common], help="Solo pasos 1–6 (login portal)")
    p_full = sub.add_parser("full", parents=[common], help="Login + enroll completo (sin OTP SMS)")
    p_full.add_argument("--phone", required=True)
    p_full.add_argument("--frente", required=True, help="Anverso INE (jpg/png/b64)")
    p_full.add_argument("--selfie", required=True, help="Selfie (jpg/png/b64)")
    p_full.add_argument("--ocr", default=None, help="ocr.json local (opcional)")
    p_full.add_argument("--back", default=None, help="Reverso INE con QRs (recomendado para GH)")
    p_full.add_argument("--label", default="")
    p_full.add_argument("--skip-biometric", action="store_true")
    p_full.add_argument("--devices", default="camera 1, facing front 480x640")

    p_state = sub.add_parser("state", help="Muestra .hubox_presencial_state.json")

    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0

    if args.cmd == "state":
        print(json.dumps(load_state(), indent=2, ensure_ascii=False))
        return 0

    user = args.user.strip()
    password = args.password
    cf = args.cf_clearance.strip() or None

    if not user or not password:
        print("Faltan HUBOX_USER / HUBOX_PASSWORD", file=sys.stderr)
        return 1

    try:
        if args.cmd == "login":
            session = login_presencial(
                user, password, cf_clearance=cf, skip_precf=args.skip_precf,
            )
            print("\n--- Login OK ---")
            print(f"Cookie: {session.cookies.get(SESSION_COOKIE, '')[:60]}…")
            return 0

        if args.cmd == "full":
            profile = _profile_from_args(args)
            result = flujo_presencial_completo(
                user,
                password,
                args.phone,
                profile,
                cf_clearance=cf,
                skip_precf=args.skip_precf,
                skip_biometric=args.skip_biometric,
                devices=args.devices,
            )
            print("\n--- Vinculación completada ---")
            print(json.dumps(
                {k: result[k] for k in result if k not in ("details",) and not (isinstance(result.get(k), str) and len(str(result[k])) > 200)},
                indent=2,
                ensure_ascii=False,
            ))
            print(f"\nEstado: {STATE_PATH.resolve()}")
            return 0

    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
