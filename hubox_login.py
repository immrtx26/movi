#!/usr/bin/env python3
"""
Login al portal Hubox Movistar (sin OTP).
Endpoint: POST /api/auth/enroll-login
Devuelve Session con cookie __Host-hubox_session (JWT, TTL 600s).
"""
import json
import base64
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

FRONTEND = "https://registro-telefonica-movistar.hubox.com"
LOGIN_URL = f"{FRONTEND}/api/auth/enroll-login"

PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAsoEUrr8XEqIRnVLtKyID
14X05OM4cBsnStN+QS6OK3X8ygB49Nt+plYLgZ18vBOq4pic7h6wK315QoqEVlWK
ixKTl42SPek0lrGXo9FT8eQzdM6fm/vJgFg/nDcXtULXnl++/wAVXvTICXDk/8bE
1IF40mcDIMQqF1+AkDUD+PV8Be0/F25Qe951N1RlWazQFopP7ARCKBQi+30g9mZJ
ftZ9w9OpMWlIFo9sOdbW3N+Em7apFHYjJzBuWgsVPEs/UahoTDu8v3laO8FDImo3
mKURWsaVMfxY7hXVskeHQ4E00KYSPVbppgCK46R3miwuMD/gHz/K8OluSbgWgb97
XwIDAQAB
-----END PUBLIC KEY-----"""


def encrypt_login_payload(payload: dict) -> str:
    pub = serialization.load_pem_public_key(PUBLIC_KEY.encode())
    json_b64 = base64.b64encode(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    ).decode()
    ct = pub.encrypt(
        json_b64.encode(),
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    return base64.b64encode(ct).decode()


def hubox_login(user: str, password: str, access_key: str = "ak_TelefonicaPruebas",
                ambiente: str = "movistar_prod") -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Mobile Safari/537.36",
        "Content-Type": "application/json",
        "Origin": FRONTEND,
        "Referer": f"{FRONTEND}/",
        "Accept": "application/json, text/plain, */*",
    })

    payload = {
        "action": "login",
        "user": user.strip(),
        "password": password,
        "ambiente": ambiente,
        "tipo": 2,
        "access_key": access_key,
    }

    r = s.post(LOGIN_URL, json={"data": encrypt_login_payload(payload)}, timeout=30)
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"Login fallo: {data}")
    return s


if __name__ == "__main__":
    s = hubox_login("EVC00346", "sJOV5ay6n<")
    print("OK - cookie:", s.cookies.get("__Host-hubox_session", "?")[:50])
