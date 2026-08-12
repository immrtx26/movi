#!/usr/bin/env python3
"""
Replay del flujo Hubox enroll Movistar (movistar_registro).

Pasos:
  1. enroll_inicio      (RSA-OAEP del payload)
  2. enroll_envia_otp
  3. enroll_valida_otp
  4. enroll_detectINE
  5. enroll_ocr
  6. enroll_qrs
  7. enroll_biometric

Ejemplos:
  python enroll_replay.py inicio --phone 5512345678
  python enroll_replay.py otp-send --track-id <uuid>
  python enroll_replay.py otp-valid --track-id <uuid> --otp 0571
  python enroll_replay.py detect-ine --img anverso.jpg
  python enroll_replay.py ocr --track-id <uuid> --img crop.jpg
  python enroll_replay.py qrs --track-id <uuid> --qr1 qr1.bin --qr2 qr2.bin
  python enroll_replay.py biometric --track-id <uuid> --far far.png --close close.png
  python enroll_replay.py full --phone 5512345678 --img-ine anverso.jpg --img-crop crop.jpg \\
      --qr1 qr1.b64 --qr2 qr2.b64 --far far.png --close close.png

Estado intermedio: .hubox_state.json (track_id, últimos responses).
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# --- Config embebida del frontend Hubox (chunk captura) ---
API_BASE = "https://api-v1.hubox.com"
ORIGIN = "https://registro-telefonica-movistar.hubox.com"
ACCESS_KEY = "ak_TelefonicaPruebas"
AMBIENTE = "movistar_prod"
FLUJO = "movistar_registro"
AES_SECRET = "1234567890111110"  # presente en frontend; enroll_inicio usa RSA

LOGIN_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAsoEUrr8XEqIRnVLtKyID
14X05OM4cBsnStN+QS6OK3X8ygB49Nt+plYLgZ18vBOq4pic7h6wK315QoqEVlWK
ixKTl42SPek0lrGXo9FT8eQzdM6fm/vJgFg/nDcXtULXnl++/wAVXvTICXDk/8bE
1IF40mcDIMQqF1+AkDUD+PV8Be0/F25Qe951N1RlWazQFopP7ARCKBQi+30g9mZJ
ftZ9w9OpMWlIFo9sOdbW3N+Em7apFHYjJzBuWgsVPEs/UahoTDu8v3laO8FDImo3
mKURWsaVMfxY7hXVskeHQ4E00KYSPVbppgCK46R3miwuMD/gHz/K8OluSbgWgb97
XwIDAQAB
-----END PUBLIC KEY-----"""

STATE_PATH = Path(".hubox_state.json")
DEFAULT_UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Mobile Safari/537.36"
)


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
        raise SystemExit(f"Telefono invalido (se esperan 10 digitos MX): {phone!r}")
    return digits


def file_to_b64(path: str | Path, raw: bool = False) -> str:
    """Lee archivo. Si raw=False y es texto base64 puro, lo usa tal cual."""
    p = Path(path)
    data = p.read_bytes()
    if not raw:
        try:
            text = data.decode("utf-8").strip()
            # parece base64 sin encabezado data-uri
            if text and all(c.isalnum() or c in "+/=\n\r" for c in text[:200]) and len(text) > 40:
                # validar decode
                base64.b64decode(text, validate=False)
                return text.replace("\n", "").replace("\r", "")
        except Exception:
            pass
    return base64.b64encode(data).decode("ascii")


def encrypt_start_payload(payload: dict[str, Any]) -> str:
    """Replica encryptJsonPayload / encryptStartPayload del frontend:
    RSA-OAEP SHA-256 sobre UTF-8(JSON), salida Base64.
    """
    public_key = serialization.load_pem_public_key(LOGIN_PUBLIC_KEY_PEM.encode())
    plaintext = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ciphertext = public_key.encrypt(
        plaintext,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(ciphertext).decode("ascii")


class HuboxClient:
    def __init__(
        self,
        access_key: str = ACCESS_KEY,
        cookie: str | None = None,
        proxy: str | None = None,
        timeout: float = 120.0,
        insecure: bool = False,
    ):
        self.access_key = access_key
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": ORIGIN,
                "Referer": f"{ORIGIN}/",
                "User-Agent": DEFAULT_UA,
            }
        )
        if cookie:
            self.session.headers["Cookie"] = cookie
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        self.session.verify = not insecure

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{API_BASE}{path}"
        print(f"\n>>> POST {url}")
        preview = {
            k: (f"<{len(v)} chars>" if isinstance(v, str) and len(v) > 120 else v)
            for k, v in body.items()
        }
        print(">>> BODY", json.dumps(preview, ensure_ascii=False))
        resp = self.session.post(url, json=body, timeout=self.timeout)
        print(f"<<< {resp.status_code}")
        try:
            data = resp.json()
        except Exception:
            print(resp.text[:800])
            resp.raise_for_status()
            raise SystemExit("Respuesta no JSON")
        # pretty print truncando blobs
        shown = {}
        for k, v in data.items():
            if isinstance(v, str) and len(v) > 200:
                shown[k] = v[:60] + f"...<{len(v)} chars>"
            elif isinstance(v, dict):
                shown[k] = v
            else:
                shown[k] = v
        print("<<< JSON", json.dumps(shown, indent=2, ensure_ascii=False))
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {json.dumps(shown, ensure_ascii=False)[:500]}")
        return data

    def inicio(self, phone: str, tipo_doc: str = "ine", flujo: str = FLUJO) -> dict[str, Any]:
        plain = {
            "numero": normalize_phone(phone),
            "flujo": flujo,
            "tipo_doc": tipo_doc,
            "access_key": self.access_key,
            "ambiente": AMBIENTE,
        }
        print("PLAINTEXT inicio:", json.dumps(plain, ensure_ascii=False))
        data = encrypt_start_payload(plain)
        out = self.post("/enroll_inicio", {"data": data})
        if out.get("track_id"):
            save_state({"track_id": out["track_id"], "phone": plain["numero"], "flujo": flujo, "inicio": out})
        return out

    def envia_otp(self, track_id: str) -> dict[str, Any]:
        out = self.post(
            "/enroll_envia_otp",
            {"track_id": track_id, "access_key": self.access_key},
        )
        save_state({"track_id": track_id, "envia_otp": out})
        return out

    def valida_otp(self, track_id: str, otp: str) -> dict[str, Any]:
        out = self.post(
            "/enroll_valida_otp",
            {"track_id": track_id, "otp": otp, "access_key": self.access_key},
        )
        save_state({"track_id": track_id, "valida_otp": out})
        return out

    def detect_ine(self, img_b64: str) -> dict[str, Any]:
        out = self.post(
            "/enroll_detectINE",
            {"img": img_b64, "access_key": self.access_key},
        )
        save_state({"detect_ine": {k: out[k] for k in out if k != "cropB64"}})
        if out.get("cropB64"):
            crop_path = Path("last_crop.jpg")
            crop_path.write_bytes(base64.b64decode(out["cropB64"]))
            print(f"crop guardado en {crop_path}")
            save_state({"crop_path": str(crop_path)})
        return out

    def ocr(self, track_id: str, img_b64: str) -> dict[str, Any]:
        out = self.post(
            "/enroll_ocr",
            {"img": img_b64, "track_id": track_id, "access_key": self.access_key},
        )
        save_state({"track_id": track_id, "ocr": out})
        return out

    def qrs(self, track_id: str, qr1_b64: str, qr2_b64: str) -> dict[str, Any]:
        out = self.post(
            "/enroll_qrs",
            {
                "qr_b64_1": qr1_b64,
                "qr_b64_2": qr2_b64,
                "track_id": track_id,
                "access_key": self.access_key,
            },
        )
        save_state({"track_id": track_id, "qrs": out})
        return out

    def biometric(
        self,
        track_id: str,
        far_b64: str,
        close_b64: str,
        devices: str = "camera 1, facing front 480x640",
        retries: int = 2,
    ) -> dict[str, Any]:
        body = {
            "farFace": far_b64,
            "closeFace": close_b64,
            "devices": devices,
            "track_id": track_id,
            "access_key": self.access_key,
        }
        last: dict[str, Any] = {}
        for attempt in range(1, retries + 1):
            try:
                last = self.post("/enroll_biometric", body)
                save_state({"track_id": track_id, "biometric": last})
                return last
            except RuntimeError as exc:
                print(f"biometric intento {attempt}/{retries} fallo: {exc}")
                if attempt >= retries:
                    raise
                time.sleep(5)
        return last


def resolve_track_id(cli_value: str | None) -> str:
    if cli_value:
        return cli_value
    st = load_state()
    tid = st.get("track_id")
    if not tid:
        raise SystemExit("Falta --track-id (o corre 'inicio' antes para guardar estado)")
    return tid


def build_client(args: argparse.Namespace) -> HuboxClient:
    return HuboxClient(
        access_key=args.access_key,
        cookie=args.cookie,
        proxy=args.proxy,
        timeout=args.timeout,
        insecure=args.insecure,
    )


def add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--access-key", default=ACCESS_KEY)
    p.add_argument("--cookie", default=None, help="Cookie completa, ej. cf_clearance=...")
    p.add_argument("--proxy", default=None, help="ej. http://127.0.0.1:8080")
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--insecure", action="store_true", help="Desactiva verify TLS")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hubox Movistar enroll replayer")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ini = sub.add_parser("inicio", help="POST /enroll_inicio")
    add_common(p_ini)
    p_ini.add_argument("--phone", required=True)
    p_ini.add_argument("--tipo-doc", default="ine")
    p_ini.add_argument("--flujo", default=FLUJO)

    p_send = sub.add_parser("otp-send", help="POST /enroll_envia_otp")
    add_common(p_send)
    p_send.add_argument("--track-id", default=None)

    p_val = sub.add_parser("otp-valid", help="POST /enroll_valida_otp")
    add_common(p_val)
    p_val.add_argument("--track-id", default=None)
    p_val.add_argument("--otp", required=True)

    p_det = sub.add_parser("detect-ine", help="POST /enroll_detectINE")
    add_common(p_det)
    p_det.add_argument("--img", required=True, help="Anverso INE (jpg/png o .b64)")

    p_ocr = sub.add_parser("ocr", help="POST /enroll_ocr")
    add_common(p_ocr)
    p_ocr.add_argument("--track-id", default=None)
    p_ocr.add_argument("--img", required=True, help="Crop INE (o last_crop.jpg)")

    p_qrs = sub.add_parser("qrs", help="POST /enroll_qrs")
    add_common(p_qrs)
    p_qrs.add_argument("--track-id", default=None)
    p_qrs.add_argument("--qr1", required=True)
    p_qrs.add_argument("--qr2", required=True)

    p_bio = sub.add_parser("biometric", help="POST /enroll_biometric")
    add_common(p_bio)
    p_bio.add_argument("--track-id", default=None)
    p_bio.add_argument("--far", required=True, help="Selfie lejana")
    p_bio.add_argument("--close", required=True, help="Selfie cercana")
    p_bio.add_argument("--devices", default="camera 1, facing front 480x640")
    p_bio.add_argument("--retries", type=int, default=2)

    p_full = sub.add_parser("full", help="Flujo interactivo completo")
    add_common(p_full)
    p_full.add_argument("--phone", required=True)
    p_full.add_argument("--tipo-doc", default="ine")
    p_full.add_argument("--flujo", default=FLUJO)
    p_full.add_argument("--otp", default=None, help="Si no se pasa, se pide por input()")
    p_full.add_argument("--img-ine", required=True)
    p_full.add_argument("--img-crop", default=None, help="Si falta, usa crop de detectINE")
    p_full.add_argument("--qr1", required=True)
    p_full.add_argument("--qr2", required=True)
    p_full.add_argument("--far", required=True)
    p_full.add_argument("--close", required=True)
    p_full.add_argument("--devices", default="camera 1, facing front 480x640")
    p_full.add_argument("--skip-biometric", action="store_true")

    p_show = sub.add_parser("state", help="Muestra .hubox_state.json")
    add_common(p_show)

    args = parser.parse_args(argv)

    if args.cmd == "state":
        print(json.dumps(load_state(), indent=2, ensure_ascii=False))
        return 0

    client = build_client(args)

    if args.cmd == "inicio":
        client.inicio(args.phone, tipo_doc=args.tipo_doc, flujo=args.flujo)
    elif args.cmd == "otp-send":
        client.envia_otp(resolve_track_id(args.track_id))
    elif args.cmd == "otp-valid":
        client.valida_otp(resolve_track_id(args.track_id), args.otp)
    elif args.cmd == "detect-ine":
        client.detect_ine(file_to_b64(args.img))
    elif args.cmd == "ocr":
        client.ocr(resolve_track_id(args.track_id), file_to_b64(args.img))
    elif args.cmd == "qrs":
        client.qrs(resolve_track_id(args.track_id), file_to_b64(args.qr1), file_to_b64(args.qr2))
    elif args.cmd == "biometric":
        client.biometric(
            resolve_track_id(args.track_id),
            file_to_b64(args.far),
            file_to_b64(args.close),
            devices=args.devices,
            retries=args.retries,
        )
    elif args.cmd == "full":
        ini = client.inicio(args.phone, tipo_doc=args.tipo_doc, flujo=args.flujo)
        tid = ini.get("track_id")
        if not tid:
            raise SystemExit("inicio no devolvio track_id")
        client.envia_otp(tid)
        otp = args.otp or input("OTP SMS: ").strip()
        client.valida_otp(tid, otp)
        det = client.detect_ine(file_to_b64(args.img_ine))
        if args.img_crop:
            crop_b64 = file_to_b64(args.img_crop)
        elif det.get("cropB64"):
            crop_b64 = det["cropB64"]
        else:
            raise SystemExit("Sin crop: pasa --img-crop")
        client.ocr(tid, crop_b64)
        client.qrs(tid, file_to_b64(args.qr1), file_to_b64(args.qr2))
        if not args.skip_biometric:
            client.biometric(
                tid,
                file_to_b64(args.far),
                file_to_b64(args.close),
                devices=args.devices,
            )
        print("\nEstado final:", STATE_PATH.resolve())
    else:
        parser.error("comando desconocido")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
