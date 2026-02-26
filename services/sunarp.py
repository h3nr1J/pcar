# services/sunarp.py
import os
import re
import io
import base64
import asyncio

from fastapi import HTTPException
from PIL import Image
from dotenv import load_dotenv, find_dotenv
from openai import OpenAI
from capmonstercloudclient import CapMonsterClient, ClientOptions
from capmonstercloudclient.requests import TurnstileRequest, ImageToTextRequest

# ========= CONFIG .env =========
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
_openai_client: OpenAI | None = None

_capmonster_client: CapMonsterClient | None = None
_capmonster_api_key: str | None = None
_sunarp_extraer_propietarios = (
    (os.getenv("SUNARP_EXTRAER_PROPIETARIOS") or "0").strip().lower() in {"1", "true", "yes", "si"}
)


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default


SUNARP_FORM_READY_TIMEOUT_MS = _env_int("SUNARP_FORM_READY_TIMEOUT_MS", 4500)
SUNARP_TURNSTILE_HOOK_TIMEOUT_MS = _env_int("SUNARP_TURNSTILE_HOOK_TIMEOUT_MS", 5000)
SUNARP_TURNSTILE_POST_SOLVE_WAIT_MS = _env_int("SUNARP_TURNSTILE_POST_SOLVE_WAIT_MS", 1200)
SUNARP_SUBMIT_OUTCOME_TIMEOUT_MS = _env_int("SUNARP_SUBMIT_OUTCOME_TIMEOUT_MS", 12000)
SUNARP_RESULT_IMAGE_TIMEOUT_MS = _env_int("SUNARP_RESULT_IMAGE_TIMEOUT_MS", 3500)
SUNARP_PROPIETARIOS_MODEL = (os.getenv("SUNARP_PROPIETARIOS_MODEL") or "gpt-4o-mini").strip()


def _get_openai_client() -> OpenAI | None:
    global _openai_client
    if _openai_client:
        return _openai_client
    api_key = (os.getenv("OPENAI_API_KEY") or OPENAI_API_KEY or "").strip()
    if not api_key:
        return None
    try:
        _openai_client = OpenAI(api_key=api_key)
    except Exception:
        _openai_client = None
    return _openai_client


def _get_capmonster_client() -> CapMonsterClient | None:
    """
    Devuelve un cliente CapMonster inicializado.

    Nota: recarga variables desde `.env` para evitar que un proceso ya levantado
    se quede sin CAPMONSTER_API_KEY si el archivo se editó después.
    """
    global _capmonster_client, _capmonster_api_key

    # Best-effort: intenta encontrar `.env` tanto desde cwd como desde el stack frame
    # (útil cuando se corre uvicorn con `--app-dir` en otro directorio).
    dotenv_path = ""
    try:
        dotenv_path = find_dotenv(usecwd=True) or ""
    except Exception:
        dotenv_path = ""
    if not dotenv_path:
        try:
            dotenv_path = find_dotenv() or ""
        except Exception:
            dotenv_path = ""
    try:
        if dotenv_path:
            load_dotenv(dotenv_path=dotenv_path, override=False)
    except Exception:
        pass

    api_key = os.getenv("CAPMONSTER_API_KEY")
    if not api_key:
        _capmonster_client = None
        _capmonster_api_key = None
        return None

    if _capmonster_client and _capmonster_api_key == api_key:
        return _capmonster_client

    _capmonster_api_key = api_key
    _capmonster_client = CapMonsterClient(options=ClientOptions(api_key=api_key))
    return _capmonster_client

# Debe cargarse la raíz de la SPA; si entras directo a /inicio el servidor devuelve 404.
URL = "https://consultavehicular.sunarp.gob.pe/consulta-vehicular/"
_turnstile_sitekey_cache: dict[str, str] = {}

# Hook para capturar el callback de Turnstile que usa la SPA.
# Importante: SUNARP no valida el captcha solo leyendo el input hidden; la app
# usa un callback JS para habilitar el flujo. Por eso capturamos la función y
# la invocamos con el token resuelto.
TURNSTILE_HOOK_SCRIPT = r"""
(() => {
  window.__pcar_turnstile = window.__pcar_turnstile || {
    widgetId: null,
    sitekey: null,
    action: null,
    cdata: null,
    token: null,
    _callback: null
  };

  const tryWrap = () => {
    try {
      const ts = window.turnstile;
      if (!ts || ts.__pcar_hooked || typeof ts.render !== 'function') return;
      ts.__pcar_hooked = true;
      const origRender = ts.render;
      ts.render = function(container, params) {
        try {
          const h = window.__pcar_turnstile;
          if (params) {
            h.sitekey = params.sitekey || params.siteKey || h.sitekey;
            h.action = params.action || params.pageAction || h.action;
            h.cdata = params.cData || params.cdata || params.data || h.cdata;
            if (typeof params.callback === 'function') h._callback = params.callback;
          }
        } catch (e) {}

        const id = origRender.call(this, container, params);
        try { window.__pcar_turnstile.widgetId = id; } catch (e) {}
        return id;
      };
    } catch (e) {}
  };

  tryWrap();
  const t = setInterval(tryWrap, 1);
  setTimeout(() => { clearInterval(t); tryWrap(); }, 15000);
})();
"""


# ============== HELPERS PLAYWRIGHT ==============

async def get_plate_input(page):
    """
    Busca el input de placa.
    En el HTML actual es: <input id="nroPlaca" ...>
    """
    # Espera mínima para que Angular/React hidrate
    try:
        await page.wait_for_selector("input", timeout=6000)
    except Exception:
        pass

    selectors = [
        "#nroPlaca",  # id clásico
        "input[name='nroPlaca']",
        "input[formcontrolname='nroPlaca']",
        "input[placeholder*='ABC123' i]",
        "input[placeholder*='Numero de Placa' i]",
        "input[placeholder*='Número de Placa' i]",
        "input[placeholder*='Placa' i]",
        "input[id*='placa' i]",
        "input[name*='placa' i]",
        "input[nz-input]",
        "input.ant-input.text-uppercase",
    ]

    # Busca en la página principal
    selectors = [
        "#nroPlaca",  # id clásico
        "input[name='nroPlaca']",
        "input[formcontrolname='nroPlaca']",
        "input[placeholder*='ABC123' i]",
        "input[placeholder*='Numero de Placa' i]",
        "input[placeholder*='Número de Placa' i]",
        "input[placeholder*='Placa' i]",
        "input[id*='placa' i]",
        "input[name*='placa' i]",
        "input[nz-input]",
        "input.ant-input.text-uppercase",
    ]

    for sel in selectors:
        loc = page.locator(sel)
        if await loc.count():
            try:
                await loc.first.wait_for(state="visible", timeout=2000)
            except Exception:
                pass
            return loc.first

    # Busca en iframes (por si Cloudflare encapsula el formulario)
    for frame in page.frames:
        for sel in selectors:
            loc = frame.locator(sel)
            if await loc.count():
                try:
                    await loc.first.wait_for(state="visible", timeout=2000)
                except Exception:
                    pass
                return loc.first

    # Último recurso: primer input de texto corto en la página
    loc = page.locator("input[type='text']")
    if await loc.count():
        try:
            await loc.first.wait_for(state="visible", timeout=2000)
        except Exception:
            pass
        return loc.first

    return None


async def get_captcha_input(page):
    """
    Busca el input del código captcha.
    Actual: <input id="codigoCaptcha" ...>
    """
    loc = page.locator("#codigoCaptcha")
    if await loc.count():
        return loc.first

    # Fallbacks
    for sel in [
        'input[formcontrolname="codigoCaptcha"]',
        'input[placeholder*="código captcha" i]',
        'input[placeholder*="codigo captcha" i]',
    ]:
        loc = page.locator(sel)
        if await loc.count():
            return loc.first

    return None


async def get_captcha_image_base64(page) -> str:
    """
    Toma screenshot del captcha (PNG) y devuelve base64.
    La imagen suele tener 'captcha' en el src.
    """
    img = page.locator("img[src*='captcha']")
    if not await img.count():
        # Fallback: cualquier img cerca del input de captcha
        captcha_input = await get_captcha_input(page)
        if captcha_input:
            loc = captcha_input.locator("xpath=preceding::img[1]")
            if await loc.count():
                img = loc.first

    if not await img.count():
        raise HTTPException(status_code=500, detail="No se encontró la imagen de captcha")

    raw_png = await img.screenshot(type="png")

    # Lo pasamos a blanco y negro para ayudar al OCR
    pil_img = Image.open(io.BytesIO(raw_png)).convert("L")
    out = io.BytesIO()
    pil_img.save(out, format="PNG")
    b64 = base64.b64encode(out.getvalue()).decode("utf-8")
    return b64


async def get_result_image_src(page) -> str | None:
    """
    Busca la imagen del resultado del vehículo (la tarjeta grande)
    y devuelve el src (data:image/png;base64,...).
    """
    loc = page.locator(".container-data-vehiculo img")
    if await loc.count():
        return await loc.first.get_attribute("src")
    # Nueva versión parece renderizar la tarjeta en DOM sin img base64.
    # Tomamos screenshot del contenedor principal como fallback.
    for sel in [
        ".card-container",
        "app-vehicular .card-container",
        "app-vehicular",
        "nz-content.body_main",
    ]:
        cont = page.locator(sel)
        if await cont.count():
            try:
                raw = await cont.first.screenshot(type="png")
                b64 = base64.b64encode(raw).decode("utf-8")
                return f"data:image/png;base64,{b64}"
            except Exception:
                continue
    return None


async def get_search_button(page):
    """
    Botón principal "Realizar Búsqueda".
    """
    selectors = [
        "button.ant-btn.btn-sunarp-green.ant-btn-primary.ant-btn-lg",
        "button:has-text('Realizar Busqueda')",
        "button:has-text('Realizar Búsqueda')",
        "button[nztype='primary']",
        "button[type='submit']",
    ]
    for sel in selectors:
        loc = page.locator(sel)
        if await loc.count():
            try:
                await loc.first.wait_for(state="visible", timeout=2000)
            except Exception:
                pass
            return loc.first
    return None


async def wait_search_form_ready(page, timeout_ms: int = SUNARP_FORM_READY_TIMEOUT_MS):
    """
    Espera de forma reactiva a que aparezca el formulario principal,
    evitando sleeps fijos largos.
    """
    elapsed = 0
    step = 250
    selectors = [
        "#nroPlaca",
        "input[name='nroPlaca']",
        "button.ant-btn.btn-sunarp-green.ant-btn-primary.ant-btn-lg",
        "button:has-text('Realizar Busqueda')",
        "button:has-text('Realizar Búsqueda')",
    ]
    while elapsed < timeout_ms:
        for sel in selectors:
            try:
                if await page.locator(sel).count():
                    return True
            except Exception:
                pass
        await page.wait_for_timeout(step)
        elapsed += step
    return False


async def wait_button_enabled(btn, page, timeout_ms: int = 12000):
    """
    Espera a que el botón no tenga disabled ni clase de loading/disabled.
    """
    elapsed = 0
    step = 500
    while elapsed < timeout_ms:
        try:
            disabled_attr = await btn.get_attribute("disabled")
            class_attr = await btn.get_attribute("class") or ""
            if not disabled_attr and "disabled" not in class_attr and "loading" not in class_attr:
                return True
        except Exception:
            try:
                await btn.wait_for(state="visible", timeout=step)
            except Exception:
                pass
        await page.wait_for_timeout(step)
        elapsed += step
    return False


async def wait_result_image_src(page, timeout_ms: int = SUNARP_RESULT_IMAGE_TIMEOUT_MS):
    """
    Espera a que se pinte la tarjeta de resultado y devuelve el src en cuanto exista.
    """
    elapsed = 0
    step = 250
    while elapsed < timeout_ms:
        try:
            src = await get_result_image_src(page)
        except Exception:
            src = None
        if src:
            return src
        await page.wait_for_timeout(step)
        elapsed += step
    return None


async def wait_security_check(page, timeout_ms: int = 8000):
    """
    La página muestra un check de Cloudflare antes de habilitar el botón.
    Esperamos a que desaparezca el estado de 'Verificando...' o se marque como exitoso.
    """
    elapsed = 0
    step = 500
    while elapsed < timeout_ms:
        try:
            ok_badge = page.locator("text=Operación exitosa")
            verifying = page.locator("text=Verificando")
            if await ok_badge.count():
                return True
            if verifying and not await verifying.count():
                return True
        except Exception:
            pass
        await page.wait_for_timeout(step)
        elapsed += step
    return False


async def wait_turnstile_token(page, timeout_ms: int = 15000):
    """
    Espera a que Cloudflare Turnstile genere el token (input/textarea cf-turnstile-response no vacío).
    Evita el mensaje de 'Captcha no resuelto'.
    """
    elapsed = 0
    step = 500
    while elapsed < timeout_ms:
        for sel in [
            "input[name='cf-turnstile-response']",
            "textarea[name='cf-turnstile-response']",
            "input[name='cf_challenge_response']",
        ]:
            loc = page.locator(sel)
            if await loc.count():
                try:
                    val = (await loc.first.input_value()).strip()
                except Exception:
                    try:
                        val = (await loc.first.get_attribute("value") or "").strip()
                    except Exception:
                        val = ""
                if val:
                    return True
        await page.wait_for_timeout(step)
        elapsed += step
    return False


async def click_turnstile_checkbox(page, timeout_ms: int = 12000):
    """
    Intenta marcar el checkbox de Cloudflare Turnstile si existe.
    """
    elapsed = 0
    step = 500
    while elapsed < timeout_ms:
        for frame in page.frames:
            url = frame.url or ""
            if "challenges.cloudflare.com" not in url and "turnstile" not in url:
                continue
            checkbox = frame.locator("input[type='checkbox'], .ctp-checkbox, label:has-text('verifica') input")
            if await checkbox.count():
                try:
                    await checkbox.first.click(force=True)
                    await frame.wait_for_timeout(800)
                    return True
                except Exception:
                    pass
        # fallback: buscar un input/checkbox fuera de iframes
        for sel in [
            "input[type='checkbox']",
            "label:has-text('Verifica que eres un ser humano') input[type='checkbox']",
        ]:
            loc = page.locator(sel)
            if await loc.count():
                try:
                    await loc.first.check()
                    await page.wait_for_timeout(800)
                    return True
                except Exception:
                    try:
                        await loc.first.click(force=True)
                        await page.wait_for_timeout(800)
                        return True
                    except Exception:
                        pass
        await page.wait_for_timeout(step)
        elapsed += step
    return False


async def _get_turnstile_hook_info(page) -> dict:
    """
    Lee (best-effort) el estado del hook de Turnstile inyectado vía init_script.
    """
    try:
        info = await page.evaluate(
            """() => {
                const h = window.__pcar_turnstile;
                if (!h) return null;
                return {
                    sitekey: h.sitekey || null,
                    action: h.action || null,
                    cdata: h.cdata || null,
                    widgetId: h.widgetId || null,
                    hasCallback: typeof h._callback === 'function',
                };
            }"""
        )
    except Exception:
        info = None
    return info or {}


async def _wait_for_turnstile_hook(page, timeout_ms: int = 15000) -> dict:
    """
    Espera a que la SPA renderice Turnstile y exponga sitekey + callback en el hook.
    """
    elapsed = 0
    step = 250
    last = {}
    while elapsed < timeout_ms:
        last = await _get_turnstile_hook_info(page)
        if last.get("sitekey") and last.get("hasCallback"):
            return last
        await page.wait_for_timeout(step)
        elapsed += step
    return last


async def _apply_turnstile_solution(page, token: str):
    """
    Aplica el token resuelto a la SPA:
    - llama al callback original de Turnstile (lo que habilita el flujo)
    - actualiza el hidden input como respaldo
    """
    try:
        await page.evaluate(
            """(token) => {
                try {
                    const h = window.__pcar_turnstile;
                    if (h) {
                        h.token = token;
                        if (typeof h._callback === 'function') {
                            h._callback(token);
                        }
                    }
                } catch (e) {}
            }""",
            token,
        )
    except Exception:
        pass

    await _inject_turnstile_token(page, token)


async def _extract_turnstile_params(page) -> dict:
    """
    Intenta leer sitekey/action/cdata del widget Turnstile en la página.
    """
    try:
        out = await page.evaluate(
            """() => {
                const out = { sitekey: null, action: null, cdata: null };
                const candidates = Array.from(document.querySelectorAll('[data-sitekey], [data-site-key]'));
                for (const el of candidates) {
                    const key = el.getAttribute('data-sitekey') || el.getAttribute('data-site-key');
                    if (key) {
                        out.sitekey = key;
                        out.action = el.getAttribute('data-action') || el.getAttribute('data-turnstile-action') || null;
                        out.cdata = el.getAttribute('data-cdata') || el.getAttribute('data-challenge') || null;
                        break;
                    }
                }
                if (!out.sitekey) {
                    const scripts = Array.from(document.querySelectorAll('script')).map(s => s.textContent || '');
                    const hit = scripts.find(txt => txt.includes('sitekey'));
                    if (hit) {
                        const m = hit.match(/sitekey\\\"?\\s*[:=]\\s*\\\"([^\\\"']+)/i) || hit.match(/['\\\"]sitekey['\\\"]\\s*[:=]\\s*['\\\"]([^'\\\"\\s]+)/i);
                        if (m) out.sitekey = m[1];
                    }
                }
                return out;
            }"""
        )
        if not out.get("sitekey"):
            # Fallback 1: intentar desde el frame de Cloudflare (suele contener .../0xSITEKEY/...)
            try:
                for fr in page.frames:
                    key = _extract_sitekey_from_cf_frame_url(fr.url or "")
                    if key:
                        out["sitekey"] = key
                        break
            except Exception:
                pass

        if not out.get("sitekey"):
            # Fallback 2: extraer desde bundles JS de la SPA
            sitekey = await _extract_turnstile_sitekey_from_assets(page)
            if sitekey:
                out["sitekey"] = sitekey
        return out
    except Exception:
        return {"sitekey": None, "action": None, "cdata": None}
async def _extract_turnstile_sitekey_from_assets(page) -> str | None:
    """
    Intenta extraer el sitekey desde los assets JS cargados por la SPA.

    SUNARP no siempre expone `data-sitekey` en el DOM; en algunos builds el sitekey
    vive dentro de un bundle (ej. `captchaCloudflare:"0x..."`).
    """
    try:
        origin = await page.evaluate("() => location.origin")
    except Exception:
        origin = ""

    if origin and origin in _turnstile_sitekey_cache:
        return _turnstile_sitekey_cache[origin]

    try:
        urls: list[str] = await page.evaluate(
            """() => {
                const urls = new Set();
                const base = document.baseURI || location.href;
                const add = (u) => {
                    if (!u) return;
                    try { urls.add(new URL(u, base).href); } catch (e) {}
                };
                document.querySelectorAll('script[src]').forEach(s => add(s.getAttribute('src')));
                document.querySelectorAll('link[rel=\"modulepreload\"][href]').forEach(l => add(l.getAttribute('href')));
                document.querySelectorAll('link[rel=\"preload\"][as=\"script\"][href]').forEach(l => add(l.getAttribute('href')));
                return Array.from(urls);
            }"""
        )
    except Exception:
        urls = []

    if not urls:
        return None

    # Filtramos solo JS del mismo origen para minimizar requests.
    if origin:
        urls = [u for u in urls if u.startswith(origin) and ".js" in u]
    else:
        urls = [u for u in urls if ".js" in u]

    # Patrones conocidos
    patterns = [
        re.compile(r"captchaCloudflare\s*[:=]\s*['\"](0x[0-9A-Za-z]+)['\"]"),
        re.compile(r"sitekey\s*[:=]\s*['\"](0x[0-9A-Za-z]+)['\"]", re.IGNORECASE),
    ]

    for url in urls[:12]:
        try:
            resp = await page.request.get(url, timeout=10_000)
            if not resp.ok:
                continue
            text = await resp.text()
        except Exception:
            continue

        for pat in patterns:
            m = pat.search(text)
            if m:
                key = m.group(1)
                if origin:
                    _turnstile_sitekey_cache[origin] = key
                return key

    return None


def _extract_sitekey_from_cf_frame_url(url: str) -> str | None:
    """
    Extrae `0x...` desde URLs del widget Turnstile de Cloudflare.
    """
    if not url:
        return None
    m = re.search(r"/(0x[0-9A-Za-z]{8,})/", url)
    return m.group(1) if m else None


async def _inject_turnstile_token(page, token: str):
    """
    Coloca el token resuelto en los inputs esperados por Turnstile.
    """
    selectors = [
        "input[name='cf-turnstile-response']",
        "textarea[name='cf-turnstile-response']",
        "input[name='cf_challenge_response']",
        "#cf-challenge-response",
        "#cf-turnstile-response",
    ]

    for sel in selectors:
        loc = page.locator(sel)
        try:
            if await loc.count():
                await loc.evaluate_all(
                    """(els, token) => {
                        els.forEach(el => {
                            el.value = token;
                            el.setAttribute('value', token);
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        });
                    }""",
                    token,
                )
        except Exception:
            continue

    # Best-effort: habilitar el botón de búsqueda si quedó disabled
    try:
        await page.evaluate(
            """() => {
                const btn = document.querySelector("button.ant-btn, button[type='submit']");
                if (!btn) return;
                btn.removeAttribute('disabled');
                btn.classList.remove('disabled', 'ant-btn-loading');
            }"""
        )
    except Exception:
        pass


async def solve_turnstile_with_capmonster(page) -> str:
    """
    Usa CapMonster para resolver el Turnstile de Cloudflare y devuelve el token.
    """
    capmonster_client = _get_capmonster_client()
    if not capmonster_client:
        raise HTTPException(
            status_code=500,
            detail="Falta CAPMONSTER_API_KEY para resolver Cloudflare Turnstile (carga el .env y reinicia la API)",
        )

    # Esperar hook en paralelo mientras extraemos parámetros: evita bloquear
    # innecesariamente cuando el sitekey ya está disponible.
    hook_task = asyncio.create_task(
        _wait_for_turnstile_hook(page, timeout_ms=SUNARP_TURNSTILE_HOOK_TIMEOUT_MS)
    )
    hook = await _get_turnstile_hook_info(page)
    try:
        params = await _extract_turnstile_params(page)
    except Exception:
        params = {}

    sitekey = hook.get("sitekey") or params.get("sitekey")
    if not sitekey:
        try:
            hook = await hook_task
        except Exception:
            hook = hook or {}
        sitekey = hook.get("sitekey") or params.get("sitekey")
    if not sitekey:
        if not hook_task.done():
            hook_task.cancel()
        raise HTTPException(status_code=500, detail="No se pudo obtener el sitekey de Turnstile")

    try:
        ua = await page.evaluate("() => navigator.userAgent")
    except Exception:
        ua = None

    req = TurnstileRequest(
        websiteURL=page.url,
        websiteKey=sitekey,
    )
    action = hook.get("action") or params.get("action")
    cdata = hook.get("cdata") or params.get("cdata")
    if action:
        req.pageAction = action
    if cdata:
        req.data = cdata
    if ua:
        req.userAgent = ua

    try:
        solution = await capmonster_client.solve_captcha(req)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error resolviendo Turnstile con CapMonster: {e}")

    token = (
        solution.get("token")
        or solution.get("cf_clearance")
        or solution.get("captchaKey")
        or ""
    )
    if not token:
        if not hook_task.done():
            hook_task.cancel()
        raise HTTPException(status_code=500, detail="CapMonster no devolvió token de Turnstile")

    # Mientras CapMonster resolvía, el hook pudo capturar el callback.
    # Le damos un margen corto adicional y luego aplicamos token.
    if not hook_task.done() and SUNARP_TURNSTILE_POST_SOLVE_WAIT_MS > 0:
        try:
            await asyncio.wait_for(
                hook_task, timeout=SUNARP_TURNSTILE_POST_SOLVE_WAIT_MS / 1000
            )
        except Exception:
            hook_task.cancel()

    await _apply_turnstile_solution(page, token)
    return token


async def _close_sunarp_captcha_modal(page):
    """
    Cierra el modal de SUNARP cuando aparece "Captcha no resuelto" o "Token Captcha Invalido".
    """
    try:
        modal = page.locator("text=Captcha no resuelto, text=Token Captcha Invalido")
        if await modal.count():
            btn_ok = page.locator("button:has-text('OK'), button:has-text('Aceptar')")
            if await btn_ok.count():
                await btn_ok.first.click()
                await page.wait_for_timeout(400)
    except Exception:
        pass


async def _remove_alert_overlays(page):
    """
    Elimina overlays/modales residuales (SweetAlert) que tapen la tarjeta.
    """
    try:
        await page.evaluate(
            """() => {
                const selectors = [
                    '.swal2-container',
                    '.swal2-backdrop-show',
                    '.swal2-shown',
                    'div[aria-label*="Captcha no resuelto" i]',
                ];
                selectors.forEach(sel => {
                    document.querySelectorAll(sel).forEach(el => el.remove());
                });
            }"""
        )
    except Exception:
        pass


async def _wait_sunarp_submit_outcome(page, timeout_ms: int = SUNARP_SUBMIT_OUTCOME_TIMEOUT_MS):
    """
    Espera el resultado del submit:
    - si aparece modal "Captcha no resuelto"
    - o si sale la respuesta de getDatosVehiculo
    """
    response_task = asyncio.create_task(
        page.wait_for_event(
            "response",
            predicate=lambda r: "getDatosVehiculo" in (r.url or ""),
            timeout=timeout_ms,
        )
    )
    modal_task = asyncio.create_task(
        page.wait_for_selector("text=Captcha no resuelto, text=Token Captcha Invalido", timeout=timeout_ms)
    )
    done, pending = await asyncio.wait(
        {response_task, modal_task},
        return_when=asyncio.FIRST_COMPLETED,
        timeout=timeout_ms / 1000,
    )

    for task in pending:
        task.cancel()

    if not done:
        return ("timeout", None)

    if response_task in done:
        try:
            return ("response", response_task.result())
        except Exception:
            return ("timeout", None)

    return ("captcha", None)


# ============== HELPERS CAPTCHA ==============

def _captcha_variants_for_ocr(captcha_b64: str) -> list[bytes]:
    """
    Devuelve variantes de la imagen para mejorar tasa de acierto OCR.
    """
    if captcha_b64.startswith("data:"):
        captcha_b64 = captcha_b64.split("base64,", 1)[-1]

    raw = base64.b64decode(captcha_b64)
    variants = [raw]

    try:
        gray = Image.open(io.BytesIO(raw)).convert("L")
        upscaled = gray.resize((gray.width * 2, gray.height * 2), resample=Image.BICUBIC)
        out = io.BytesIO()
        upscaled.save(out, format="PNG")
        variants.append(out.getvalue())
    except Exception:
        pass

    return variants


async def solve_captcha_with_capmonster(captcha_b64: str) -> str:
    """
    Resuelve captcha de imagen (texto) usando CapMonster ImageToText.
    """
    capmonster_client = _get_capmonster_client()
    if not capmonster_client:
        raise HTTPException(
            status_code=500,
            detail="Falta CAPMONSTER_API_KEY para resolver captcha de imagen",
        )

    for img_bytes in _captcha_variants_for_ocr(captcha_b64):
        req = ImageToTextRequest(
            image_bytes=img_bytes,
            module_name="universal",
            numeric=0,
            case=False,
            math=False,
        )
        try:
            solution = await asyncio.wait_for(capmonster_client.solve_captcha(req), timeout=15)
        except Exception:
            continue

        raw = (
            (solution or {}).get("text")
            or (solution or {}).get("answer")
            or (solution or {}).get("code")
            or ""
        )
        cleaned = re.sub(r"[^A-Za-z0-9]", "", str(raw)).upper()
        if cleaned:
            return cleaned

    raise HTTPException(status_code=500, detail="CapMonster no pudo resolver captcha de imagen")


def _parse_propietario_nombre(nombre: str) -> dict:
    """
    Intenta separar un nombre de propietario en:
    - ap_paterno
    - ap_materno
    - nombres

    Ejemplos esperados:
      "OJEDA CHAMORRO, WILBERT" -> ap_paterno=OJEDA, ap_materno=CHAMORRO, nombres=WILBERT
      "PEREZ GARCIA JUAN CARLOS" -> ap_paterno=PEREZ, ap_materno=GARCIA, nombres=JUAN CARLOS
    """
    original = nombre
    nombre = nombre.strip()
    if not nombre:
        return {"texto": original, "ap_paterno": "", "ap_materno": "", "nombres": ""}

    # Caso con coma: "APELLIDO1 APELLIDO2, NOMBRES"
    ap_paterno = ap_materno = nombres = ""
    if "," in nombre:
        apellidos, nombres = [p.strip() for p in nombre.split(",", 1)]
        ap_tokens = apellidos.split()
        if ap_tokens:
            ap_paterno = ap_tokens[0]
            ap_materno = " ".join(ap_tokens[1:]).strip()
        nombres = nombres.strip()
    else:
        tokens = nombre.split()
        if len(tokens) >= 3:
            ap_paterno = tokens[0]
            ap_materno = tokens[1]
            nombres = " ".join(tokens[2:]).strip()
        elif len(tokens) == 2:
            ap_paterno, ap_materno = tokens
            nombres = ""
        elif len(tokens) == 1:
            ap_paterno = tokens[0]
            nombres = ""

    return {
        "texto": original,
        "ap_paterno": ap_paterno.strip(),
        "ap_materno": ap_materno.strip(),
        "nombres": nombres.strip(),
    }


async def extract_propietarios_from_image(image_b64: str) -> list[str]:
    """
    Extrae los nombres de propietario(s) desde la imagen que devuelve SUNARP.
    Devuelve una lista de nombres tal como aparecen (en mayúsculas generalmente).
    """
    if image_b64.startswith("data:"):
        # data:image/png;base64,<...>
        image_b64 = image_b64.split("base64,", 1)[-1]

    client = _get_openai_client()
    if not client:
        return []

    data_url = f"data:image/png;base64,{image_b64}"

    def _call():
        resp = client.responses.create(
            model=SUNARP_PROPIETARIOS_MODEL,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Extrae únicamente los nombres completos de propietario(s) que aparecen "
                                "en la imagen SUNARP. Si hay más de uno, devuelve uno por línea."
                            ),
                        },
                        {"type": "input_image", "image_url": data_url},
                    ],
                }
            ],
            instructions=(
                "Responde solo con nombres de personas o razón social propietaria, "
                "uno por línea, sin numeración, sin etiquetas, sin comentarios."
            ),
        )
        return resp.output_text

    try:
        raw = await asyncio.to_thread(_call)
    except Exception as e:
        print("Error extrayendo propietarios (OpenAI):", e)
        return []

    propietarios: list[str] = []
    vistos: set[str] = set()
    basura = (
        "PLACA",
        "PARTIDA",
        "MODELO",
        "MOTOR",
        "SERIE",
        "VIN",
        "TARJETA",
        "ASIENTO",
        "REGISTRO",
        "SUNARP",
        "PROPIETARIO",
    )
    for line in raw.replace(";", "\n").splitlines():
        clean = re.sub(r"\s+", " ", (line or "").strip(" :\t\r\n")).upper()
        if len(clean) < 3:
            continue
        if any(b in clean for b in basura):
            continue
        if re.search(r"\d{5,}", clean):
            continue
        if not re.fullmatch(r"[A-Z0-9ÁÉÍÓÚÜÑ.,'\- ]+", clean):
            continue
        if clean in vistos:
            continue
        vistos.add(clean)
        propietarios.append(clean)
    return propietarios


# ============== FUNCIÓN PRINCIPAL ==============

async def consulta_sunarp(
    placa: str,
    browser,
    extraer_propietarios: bool | None = None,
    incluir_imagen: bool = True,
):
    """
    Hace TODO el flujo de SUNARP.
    Es básicamente tu antiguo endpoint /consulta-vehicular,
    pero convertido en función reutilizable.
    """
    context = await browser.new_context(
        locale="es-PE",
        ignore_https_errors=True,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        viewport={"width": 1366, "height": 768},
    )
    await context.add_init_script(script=TURNSTILE_HOOK_SCRIPT)
    page = await context.new_page()

    # 1) Ir a la página (SPA). Si vas directo a /inicio, devuelve 404; por eso usamos la raíz.
    await page.goto(URL, wait_until="domcontentloaded")
    await wait_search_form_ready(page, timeout_ms=SUNARP_FORM_READY_TIMEOUT_MS)

    # 2) Rellenar placa
    placa = placa.strip().upper()
    plate_input = await get_plate_input(page)
    if not plate_input:
        await context.close()
        raise HTTPException(status_code=500, detail="No se encontró el input de placa en la página")

    await plate_input.fill(placa)

    # 3) Si existe captcha, resolver; si no, resolver Turnstile con CapMonster
    captcha_text = ""
    turnstile_token = ""
    captcha_input = await get_captcha_input(page)
    if captcha_input:
        captcha_b64 = await get_captcha_image_base64(page)
        captcha_text = await solve_captcha_with_capmonster(captcha_b64)
        await captcha_input.fill(captcha_text)

    # 4) CLICK EN EL BOTÓN "Realizar Búsqueda"
    btn = await get_search_button(page)
    if not btn:
        await context.close()
        raise HTTPException(status_code=500, detail="No se encontró el botón de búsqueda")

    submit_response = None
    for attempt in range(3):
        # Para Turnstile: resolver justo antes de hacer submit (token expira rápido)
        if not captcha_input:
            try:
                turnstile_token = await solve_turnstile_with_capmonster(page)
            except HTTPException:
                await context.close()
                raise
            except Exception as e:
                print("Turnstile (CapMonster) falló, intentando checkbox:", e)
                await wait_security_check(page)
                await click_turnstile_checkbox(page)
                await wait_turnstile_token(page)

        # Si quedó algún modal abierto de intentos previos, cerrarlo antes de click
        await _close_sunarp_captcha_modal(page)
        await wait_button_enabled(btn, page)
        try:
            await btn.click()
        except Exception:
            # Si un overlay (SweetAlert) intercepta el click, forzamos después de cerrar modal
            await _close_sunarp_captcha_modal(page)
            await btn.click(force=True)

        outcome, resp = await _wait_sunarp_submit_outcome(
            page, timeout_ms=SUNARP_SUBMIT_OUTCOME_TIMEOUT_MS
        )
        if outcome == "response":
            # SUNARP puede responder 200 pero indicar "Token Captcha Invalido"
            try:
                payload = await resp.json()
            except Exception:
                payload = None

            if isinstance(payload, dict):
                cod = payload.get("cod")
                mensaje = (payload.get("mensaje") or "") + " " + (payload.get("mensajeTxt") or "")
                if cod == 1:
                    submit_response = resp
                    break
                if "token captcha invalido" in mensaje.lower():
                    await _close_sunarp_captcha_modal(page)
                    await page.wait_for_timeout(400)
                    continue

            submit_response = resp
            break
        if outcome == "captcha":
            await _close_sunarp_captcha_modal(page)
            await page.wait_for_timeout(400)
            continue
        # timeout: reintento
        await page.wait_for_timeout(600)

    # 5) Esperar resultado (aunque no hayamos capturado submit_response, intentamos leer UI)
    body_text = ""
    result_img_src = None
    try:
        await _close_sunarp_captcha_modal(page)
        await _remove_alert_overlays(page)
        result_img_src = await wait_result_image_src(page, timeout_ms=SUNARP_RESULT_IMAGE_TIMEOUT_MS)
        if not result_img_src:
            await _remove_alert_overlays(page)
            result_img_src = await get_result_image_src(page)
        body_text = await page.inner_text("body")
    except Exception:
        pass

    if not submit_response and not result_img_src:
        await context.close()
        raise HTTPException(status_code=500, detail="SUNARP: captcha no resuelto / sin respuesta del servicio")
    if not result_img_src:
        await context.close()
        raise HTTPException(status_code=500, detail="No se pudo obtener el resultado de la consulta")

    await context.close()

    should_extract_propietarios = (
        _sunarp_extraer_propietarios if extraer_propietarios is None else bool(extraer_propietarios)
    )

    # Si todo bien, devolvemos texto e imagen.
    # Por defecto NO extraemos propietarios para evitar latencia extra.
    propietarios = []
    if should_extract_propietarios and result_img_src:
        propietarios = await extract_propietarios_from_image(result_img_src)
    propietarios_detalle = [_parse_propietario_nombre(p) for p in propietarios]

    return {
        "ok": True,
        "placa": placa,
        "captcha_detectado": captcha_text or turnstile_token or None,
        "captcha_valido": True,
        "resultado_crudo": body_text,
        "imagen_resultado_src": result_img_src if incluir_imagen else None,
        "propietarios": propietarios,
        "propietarios_detalle": propietarios_detalle,
        "propietarios_extraidos": should_extract_propietarios,
    }


async def enriquecer_resultado_sunarp_con_propietarios(data: dict | None) -> dict:
    """
    Toma un resultado de `consulta_sunarp` y completa propietarios desde la imagen
    usando OpenAI, solo si aún no existen.
    """
    out = dict(data or {})
    propietarios_detalle = out.get("propietarios_detalle") or []
    if propietarios_detalle:
        out["propietarios_extraidos"] = True
        return out

    src = (out.get("imagen_resultado_src") or "").strip()
    if not src:
        out.setdefault("propietarios", [])
        out.setdefault("propietarios_detalle", [])
        out["propietarios_extraidos"] = False
        return out

    propietarios = await extract_propietarios_from_image(src)
    out["propietarios"] = propietarios
    out["propietarios_detalle"] = [_parse_propietario_nombre(p) for p in propietarios]
    out["propietarios_extraidos"] = True
    return out
