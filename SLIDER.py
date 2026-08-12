import asyncio, logging, os, sys, time, random, re, json, uuid
from datetime import datetime, timezone
from typing import Optional

import requests
from playwright.async_api import async_playwright, TimeoutError as PwTimeout, Error as PwError

logger = logging.getLogger(__name__)

ATT_ORIGIN = "https://www.att.com.mx"
ATT_PORTAL = "vinculatulinea"
ATT_PORTAL_URL = f"{ATT_ORIGIN}/{ATT_PORTAL}/"
ATT_API_BASE = f"{ATT_ORIGIN}/{ATT_PORTAL}/api"



BROWSER_INIT_JS = r"""
(() => {
  try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch (e) {}
  const orig = CanvasRenderingContext2D.prototype.fillText;
  CanvasRenderingContext2D.prototype.fillText = function (t) {
    const s = String(t);
    if (/^\d{1,2}$/.test(s)) {
      const now = Date.now();
      // Si pinta dígitos seguidos (ej. "2" luego "6"), concatenar
      if (window.__captchaTargetTs && (now - window.__captchaTargetTs) < 80
          && /^\d$/.test(String(window.__captchaTarget || '')) && /^\d$/.test(s)) {
        window.__captchaTarget = String(window.__captchaTarget) + s;
      } else {
        window.__captchaTarget = s;
      }
      window.__captchaTargetTs = now;
    }
    return orig.apply(this, arguments);
  };
})();
"""

_SOLVE_SLIDER_JS = r"""
(() => {
  const r = document.querySelector('#myRange');
  if (!r) return 'no-slider';
  const target = window.__captchaTarget;
  if (target == null) return 'no-target';

  const val = String(target);
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  const fire = (type, extra) => {
    try {
      r.dispatchEvent(new Event(type, { bubbles: true, cancelable: true, ...(extra || {}) }));
    } catch (e) {}
  };
  const firePointer = (type) => {
    try {
      r.dispatchEvent(new PointerEvent(type, {
        bubbles: true, cancelable: true, pointerId: 1, pointerType: 'touch',
        isPrimary: true, clientX: 200, clientY: 400,
      }));
    } catch (e) {}
  };
  const fireMouse = (type) => {
    try {
      r.dispatchEvent(new MouseEvent(type, {
        bubbles: true, cancelable: true, view: window, clientX: 200, clientY: 400,
      }));
    } catch (e) {}
  };
  const fireTouch = (type) => {
    try {
      const t = new Touch({ identifier: 1, target: r, clientX: 200, clientY: 400 });
      r.dispatchEvent(new TouchEvent(type, {
        bubbles: true, cancelable: true, touches: type === 'touchend' ? [] : [t],
        targetTouches: type === 'touchend' ? [] : [t], changedTouches: [t],
      }));
    } catch (e) {}
  };

  // Simular arrastre real: pointer/mouse/touch down → valor → move → up
  firePointer('pointerdown');
  fireMouse('mousedown');
  fireTouch('touchstart');

  const min = Number(r.min || 0);
  const max = Number(r.max || 100);
  const goal = Number(val);
  const steps = 6;
  for (let i = 1; i <= steps; i++) {
    const v = Math.round(min + ((goal - min) * i) / steps);
    setter.call(r, String(v));
    fire('input');
    firePointer('pointermove');
    fireMouse('mousemove');
  }
  setter.call(r, val);
  fire('input');
  fire('change');
  firePointer('pointerup');
  fireMouse('mouseup');
  fireTouch('touchend');

  try {
    if (window.jQuery) {
      window.jQuery(r).val(val).trigger('input').trigger('change').trigger('mouseup');
    }
  } catch (e) {}

  // Botón de verificación del slider si existe
  const btn = document.querySelector(
    '#btnVerify, #verify, .btn-verify, button.verify, button[onclick*="verif"], button[onclick*="check"]'
  );
  if (btn) {
    try { btn.click(); } catch (e) {}
  }

  // Algunos WAF escuchan submit del form padre
  const form = r.closest('form');
  if (form) {
    try {
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    } catch (e) {}
  }

  return 'solved:' + val;
})();
"""

_IS_BLANK_PAGE_JS = r"""
() => {
  try {
    const body = document.body;
    if (!body) return true;
    if (document.querySelector('#myRange')) return false;
    if (document.querySelector('input[type="tel"], input[name*="msisdn"], input[name*="phone"]')) return false;
    if (document.querySelector('button, a[href], form input')) {
      const text = (body.innerText || '').replace(/\s+/g, ' ').trim();
      if (text.length > 40) return false;
    }
    const text = (body.innerText || '').replace(/\s+/g, ' ').trim();
    const htmlLen = (body.innerHTML || '').length;
    // Pantalla blanca / casi vacía tras el challenge
    if (text.length < 25 && htmlLen < 800) return true;
    if (text.length < 15) return true;
    return false;
  } catch (e) {
    return true;
  }
}
"""

_WAIT_CAPTCHA_SOLVED_JS = r"""
() => {
  const recaptcha = document.querySelector('#g-recaptcha-response');
  if (recaptcha && recaptcha.value && recaptcha.value.length > 20) return 'recaptcha';
  const checked = document.querySelector('.recaptcha-checkbox-checked');
  if (checked) return 'recaptcha';
  const turnstile = document.querySelector('[name="cf-turnstile-response"]');
  if (turnstile && turnstile.value && turnstile.value.length > 10) return 'turnstile';
  const hcaptcha = document.querySelector('textarea[data-hcaptcha-response]');
  if (hcaptcha && hcaptcha.value && hcaptcha.value.length > 10) return 'hcaptcha';

  // Solo captchas reales de terceros (NO el slider #myRange de AT&T)
  const frames = document.querySelectorAll(
    'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="challenges.cloudflare.com"], iframe[src*="cf-turnstile"]'
  );
  if (frames.length > 0) return 'pending';

  const containers = document.querySelectorAll('.g-recaptcha, .h-captcha, .cf-turnstile');
  if (containers.length > 0) return 'pending';

  return 'none';
}
"""

_IS_PAGE_ALIVE_JS = r"""
() => {
  return document && document.body && document.readyState;
}
"""


class ActivationError(Exception):
    pass


class PageCrashedError(Exception):
    pass


class CaptchaTimeoutError(Exception):
    pass


class WAFError(Exception):
    pass


class OTPRequiredError(Exception):
    def __init__(self, message="OTP_SENT"):
        self.message = message
        super().__init__(message)


_EXTENSION_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "Capmonster"))
_PROFILE_DIR = os.path.join(os.path.dirname(__file__), ".chrome_profile")

_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-web-security",
    "--disable-features=IsolateOrigins,site-per-process",
    f"--disable-extensions-except={_EXTENSION_PATH}",
    f"--load-extension={_EXTENSION_PATH}",
]

_CONTEXT_OPTIONS = {
    "locale": "es-MX",
    "user_agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Mobile Safari/537.36",
    "viewport": {"width": 412, "height": 915},
    "is_mobile": True,
    "has_touch": True,
}


def leer_proxies(archivo="proxies.txt"):
    try:
        with open(archivo, "r", encoding="utf-8-sig") as f:
            return [l.strip() for l in f if l.strip() and not l.startswith("#")]
    except FileNotFoundError:
        return []


def _parse_proxy(proxy_str):
    if not proxy_str:
        return None
    parts = proxy_str.split(":")
    if len(parts) == 4:
        host, port, user, password = parts
        return {"server": f"http://{host}:{port}", "username": user, "password": password}
    if len(parts) == 3:
        host, port, user = parts
        return {"server": f"http://{host}:{port}", "username": user, "password": ""}
    if len(parts) == 2:
        host, port = parts
        return {"server": f"http://{host}:{port}"}
    return {"server": f"http://{proxy_str}"}


try:
    from proxy_manager import get_proxy as _get_rotated_proxy, get_rotator as _get_rotator, proxy_to_playwright as _proxy_to_pw
    _HAS_ROTATOR = True
except ImportError:
    _HAS_ROTATOR = False


def _utc_timestamp():
    now = datetime.now(timezone.utc)
    ms = now.microsecond // 1000
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms:03d}Z"


async def _click_if_visible(page, selector, timeout=5000):
    try:
        el = page.locator(selector).first
        if await el.count() and await el.is_visible():
            await el.click(timeout=timeout)
            return True
    except Exception:
        try:
            el = page.locator(selector).first
            if await el.count():
                await el.click(force=True, timeout=timeout)
                return True
        except Exception:
            pass
    return False


async def _fill_if_visible(page, selector, value, timeout=5000):
    try:
        el = page.locator(selector).first
        if await el.count() and await el.is_visible():
            await el.fill(value, timeout=timeout)
            return True
    except Exception:
        pass
    return False


async def _remove_overlays(page):
    """Quitar solo overlays de bloqueo obvios, no la UI del portal."""
    try:
        await page.evaluate(
            """() => {
              document.querySelectorAll(
                '.modal-backdrop, .cookie-banner, #onetrust-banner-sdk, .onetrust-pc-dark-filter'
              ).forEach(el => el.remove());
            }"""
        )
    except Exception:
        pass


async def _is_page_alive(page):
    try:
        result = await page.evaluate(_IS_PAGE_ALIVE_JS)
        return result is not None
    except Exception:
        return False


async def _solve_slider(page):
    try:
        target = None
        for _ in range(10):
            target = await page.evaluate("() => window.__captchaTarget")
            has_slider = await page.evaluate("() => !!document.querySelector('#myRange')")
            if has_slider and target is not None:
                break
            await asyncio.sleep(0.35)

        if not has_slider:
            return None
        if target is None:
            logger.warning("Slider presente pero sin __captchaTarget aún")
            return False

        target_str = str(target)
        slider = page.locator("#myRange").first

        # 1) Intento nativo Playwright fill (dispara input handlers reales)
        try:
            await slider.focus(timeout=2000)
            await slider.fill(target_str, timeout=3000)
            await slider.dispatch_event("change")
        except Exception:
            pass

        # 2) Arrastre real del thumb con mouse
        try:
            box = await slider.bounding_box()
            if box:
                min_v, max_v, goal = await page.evaluate(
                    """() => {
                      const r = document.querySelector('#myRange');
                      return [Number(r.min||0), Number(r.max||100), Number(window.__captchaTarget)];
                    }"""
                )
                span = max(max_v - min_v, 1)
                ratio = (goal - min_v) / span
                y = box["y"] + box["height"] / 2
                x0 = box["x"] + 4
                x1 = box["x"] + box["width"] * ratio
                await page.mouse.move(x0, y)
                await page.mouse.down()
                steps = 8
                for i in range(1, steps + 1):
                    await page.mouse.move(x0 + (x1 - x0) * i / steps, y, steps=1)
                    await page.wait_for_timeout(30)
                await page.mouse.up()
        except Exception as e:
            logger.debug("Drag slider falló: %s", e)

        # 3) Fallback JS (setter + eventos) por si el drag no alcanzó el valor exacto
        res = await page.evaluate(_SOLVE_SLIDER_JS)
        if isinstance(res, str) and res.startswith("solved:"):
            logger.info("Slider resuelto -> %s", res.split(":")[1])
        else:
            logger.info("Slider valor forzado -> %s", target_str)

        # Confirmar valor en el DOM
        cur = await page.evaluate("() => document.querySelector('#myRange')?.value")
        if str(cur) != target_str:
            logger.warning("Valor slider=%s (esperado %s), reintentando setter", cur, target_str)
            await page.evaluate(_SOLVE_SLIDER_JS)

        return True
    except Exception as e:
        logger.warning("Error en slider: %s", e)
        return False


async def _is_blank_page(page):
    try:
        return bool(await page.evaluate(_IS_BLANK_PAGE_JS))
    except Exception:
        return False


async def _slider_value_matches(page):
    try:
        return bool(await page.evaluate(
            """() => {
              const r = document.querySelector('#myRange');
              if (!r || window.__captchaTarget == null) return false;
              return String(r.value) === String(window.__captchaTarget);
            }"""
        ))
    except Exception:
        return False


async def _click_start_buttons(page):
    """Click 'Sí comenzar' — el slider AT&T NO desaparece solo; hay que pulsar el botón."""
    selectors = (
        'afc-button[aria-label="boton comenzar"] button',
        'afc-button[aria-label="boton comenzar"]',
        'button:has-text("comenzar")',
        'span.afcTextoA:has-text("comenzar")',
        'button:has-text("Continuar")',
        'afc-button[aria-label="boton validar"] button',
        'button:has-text("Validar")',
    )
    for btn_sel in selectors:
        try:
            btn = page.locator(btn_sel).first
            if await btn.count() == 0:
                continue
            if await btn.is_visible():
                await btn.click(timeout=3000, force=True)
                logger.info("Click en: %s", btn_sel)
                return True
        except Exception:
            continue
    try:
        clicked = await page.evaluate(
            """() => {
              const el = document.querySelector('afc-button[aria-label="boton comenzar"] button')
                || document.querySelector('afc-button[aria-label="boton comenzar"]');
              if (el) { el.click(); return 'boton comenzar'; }
              const spans = [...document.querySelectorAll('span.afcTextoA, button')];
              const t = spans.find(s => /comenzar|validar|continuar/i.test(s.textContent || ''));
              if (t) { (t.closest('button') || t).click(); return (t.textContent || '').trim(); }
              return null;
            }"""
        )
        if clicked:
            logger.info("Click JS en: %s", clicked)
            return True
    except Exception:
        pass
    return False


async def _wait_after_slider(page, portal_url, max_wait=25):
    """
    Tras poner el slider en el número → click 'Sí comenzar'
    y esperar Incode / OTP / siguiente pantalla (el #myRange no desaparece solo).
    """
    if not await _slider_value_matches(page):
        await _solve_slider(page)
        await page.wait_for_timeout(400)

    clicked = await _click_start_buttons(page)
    if not clicked:
        logger.warning("No se pudo clickear 'Sí comenzar'")
    else:
        await page.wait_for_timeout(2000)

    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        try:
            cur = page.url or ""
            if INCODE_WORKFLOW_HOST in cur and "/workflow/" in cur:
                return "incode"
        except Exception:
            pass

        try:
            state = await page.evaluate(
                """() => {
                  const t = (document.body && document.body.innerText) || '';
                  if (/ya est[a\\u00e1] vinculado/i.test(t)) return 'vinculado';
                  if (/OTP|c[o\\u00f3]digo|expira/i.test(t) && /validar/i.test(t)) return 'otp';
                  if (/One More Step|Checking your browser/i.test(t)) return 'waf';
                  if (/deslizar el bot[o\\u00f3]n/i.test(t)) return 'slide';
                  if (!t || t.replace(/\\s+/g,'').length < 20) return 'blank';
                  return 'other';
                }"""
            )
        except Exception:
            state = None

        if state == "vinculado":
            logger.info("Número ya vinculado (UI)")
            return "vinculado"
        if state == "otp":
            logger.info("UI de OTP detectada tras Sí comenzar")
            return "otp"
        if state == "incode" or INCODE_WORKFLOW_HOST in (page.url or ""):
            return "incode"
        if state == "waf":
            logger.warning("WAF tras Sí comenzar")
            break
        if state == "other":
            await _click_start_buttons(page)
            await page.wait_for_timeout(1500)
            if INCODE_WORKFLOW_HOST in (page.url or ""):
                return "incode"
            return "ok"
        if state == "slide":
            if not await _slider_value_matches(page):
                await _solve_slider(page)
                await page.wait_for_timeout(300)
            await _click_start_buttons(page)
        if state == "blank":
            break

