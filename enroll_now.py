#!/usr/bin/env python3
import json, base64, time, sys
from pathlib import Path
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

FRONTEND = "https://registro-telefonica-movistar.hubox.com"
API_BASE = "https://api-v1.hubox.com"
INE_API = "https://ine-services-2026.hubox.com/ine-services"
LOGIN_URL = f"{FRONTEND}/api/auth/enroll-login"
PHONE = "6635394149"
ACCESS_KEY = "ak_TelefonicaPruebas"
AMBIENTE = "movistar_prod"

PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAsoEUrr8XEqIRnVLtKyID
14X05OM4cBsnStN+QS6OK3X8ygB49Nt+plYLgZ18vBOq4pic7h6wK315QoqEVlWK
ixKTl42SPek0lrGXo9FT8eQzdM6fm/vJgFg/nDcXtULXnl++/wAVXvTICXDk/8bE
1IF40mcDIMQqF1+AkDUD+PV8Be0/F25Qe951N1RlWazQFopP7ARCKBQi+30g9mZJ
ftZ9w9OpMWlIFo9sOdbW3N+Em7apFHYjJzBuWgsVPEs/UahoTDu8v3laO8FDImo3
mKURWsaVMfxY7hXVskeHQ4E00KYSPVbppgCK46R3miwuMD/gHz/K8OluSbgWgb97
XwIDAQAB
-----END PUBLIC KEY-----"""


def encrypt_start(payload):
    pub = serialization.load_pem_public_key(PUBLIC_KEY.encode())
    plaintext = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ct = pub.encrypt(plaintext, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    return base64.b64encode(ct).decode()


def encrypt_login(payload):
    pub = serialization.load_pem_public_key(PUBLIC_KEY.encode())
    json_b64 = base64.b64encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()).decode()
    ct = pub.encrypt(json_b64.encode(), padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    return base64.b64encode(ct).decode()


def hubox_login(user, password):
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36",
        "Content-Type": "application/json", "Origin": FRONTEND,
        "Referer": f"{FRONTEND}/", "Accept": "application/json, text/plain, */*",
    })
    payload = {"action": "login", "user": user, "password": password, "ambiente": AMBIENTE, "tipo": 2, "access_key": ACCESS_KEY}
    r = s.post(LOGIN_URL, json={"data": encrypt_login(payload)}, timeout=30)
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"Login fallo: {data}")
    print("Login OK")
    return s


def b64f(path):
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


def post_api(session, path, body, base=API_BASE, timeout=120):
    r = session.post(f"{base}{path}", json=body, timeout=timeout)
    data = r.json()
    short = {k: (f"<{len(v)}b>" if isinstance(v,str) and len(v)>100 else v) for k,v in data.items()}
    print(f"  {path}: {r.status_code} {json.dumps(short, ensure_ascii=False)[:300]}")
    return data


def build_biograficos(ocr):
    a1 = str(ocr.get("apellido_paterno") or "").strip()
    a2 = str(ocr.get("apellido_materno") or "").strip()
    noms = str(ocr.get("nombre_pila") or "").strip()
    if not (a1 and noms):
        parts = ocr["nombre"].split()
        a1 = parts[0] if len(parts) > 0 else ""
        a2 = parts[1] if len(parts) > 1 else ""
        noms = " ".join(parts[2:]) if len(parts) > 2 else ""
    curp = ocr["curp"]
    sm = {"AS":"01","BC":"02","BS":"03","CC":"04","CS":"05","CH":"06","CO":"07","CL":"08","DF":"09","DG":"10","GT":"11","GR":"12","HG":"13","JC":"14","MC":"15","MN":"16","MS":"17","NT":"18","NL":"19","OC":"20","PL":"21","QT":"22","QR":"23","SP":"24","SL":"25","SR":"26","TC":"27","TS":"28","TL":"29","VZ":"30","YN":"31","ZS":"32"}
    eid = sm.get(curp[11:13], "")
    vigencia = str(ocr.get("vigencia") or "2020-2030").strip() or "2020-2030"
    return "|".join(["I",vigencia,curp,"","",ocr["clave_elector"],noms,a1,a2,ocr["direccion"],"","",eid,"","",ocr.get("genero","M"),"","","",time.strftime("%Y%m%d"),""])


def main():
    ocr = json.loads(Path("ESCALANTE_ANEL_DAMARIX_ocr.json").read_text())
    print(f"Perfil: {ocr['nombre']}")

    print("\n=== 1. LOGIN ===")
    s = hubox_login("EVC00346", "sJOV5ay6n<")

    print("\n=== 2. INICIO (proxy) ===")
    ini_plain = {"numero": PHONE, "flujo": "movistar_registro", "tipo_doc": "ine", "access_key": ACCESS_KEY, "ambiente": AMBIENTE}
    r = s.post(f"{FRONTEND}/api/auth/enroll-inicio", json={"data": encrypt_start(ini_plain)}, timeout=30)
    ini = r.json()
    tid = ini.get("track_id")
    if not tid:
        raise SystemExit(f"No track_id: {ini}")
    print(f"  track_id: {tid}")

    print("\n=== 2b. ENVIA OTP ===")
    post_api(s, "/enroll_envia_otp", {"track_id": tid, "access_key": ACCESS_KEY})

    print("\n=== 2c. VALIDA OTP (skip con portal session) ===")
    post_api(s, "/enroll_valida_otp", {"track_id": tid, "otp": "0000", "access_key": ACCESS_KEY})

    print("\n=== 3. DETECT INE (1ra) ===")
    post_api(s, "/enroll_detectINE", {"img": b64f("FRENTE.jpeg"), "access_key": ACCESS_KEY}, timeout=180)

    print("\n=== 4. DETECT INE (2da) ===")
    det = post_api(s, "/enroll_detectINE", {"img": b64f("FRENTE.jpeg"), "access_key": ACCESS_KEY}, timeout=180)
    crop_b64 = det.get("cropB64")

    print("\n=== 5. OCR ===")
    ocr_resp = post_api(s, "/enroll_ocr", {"img": crop_b64, "track_id": tid, "access_key": ACCESS_KEY}, timeout=180)

    print("\n=== 6. GENERA QRS ===")
    biograficos = build_biograficos(ocr)
    r = requests.post(f"{INE_API}/genera-qrs", json={"biograficos": biograficos, "fotografia": crop_b64, "huellas": []}, timeout=30)
    resp = r.json()
    if resp.get("estatus") != 0:
        raise SystemExit(f"genera-qrs fallo: {resp}")
    qr1, qr2 = resp["bytesQrs"][0], resp["bytesQrs"][1]
    print(f"  QRs: {len(qr1)}b, {len(qr2)}b")

    print("\n=== 7. ENROLL QRS ===")
    post_api(s, "/enroll_qrs", {"qr_b64_1": qr1, "qr_b64_2": qr2, "track_id": tid, "access_key": ACCESS_KEY}, timeout=180)

    print("\n=== 8. BIOMETRIC ===")
    bio = post_api(s, "/enroll_biometric", {"farFace": b64f("SELFIE.jpeg"), "closeFace": b64f("SELFIE.jpeg"), "devices": "camera 1, facing front 480x640", "track_id": tid, "access_key": ACCESS_KEY}, timeout=300)

    print(f"\n=== RESULTADO === track_id: {tid}")
    print(json.dumps(bio, indent=2, ensure_ascii=False)[:1000])


if __name__ == "__main__":
    main()
