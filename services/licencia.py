import io
import asyncio
import base64
import os
import re
import uuid
from dataclasses import dataclass
from time import monotonic
from fastapi import HTTPException
from PIL import Image, ImageFilter, ImageOps
from dotenv import load_dotenv, find_dotenv
from openai import OpenAI
from capmonstercloudclient import CapMonsterClient, ClientOptions
from capmonstercloudclient.requests import ImageToTextRequest

# Config .env (best-effort)
load_dotenv()

# URL del Sistema de Licencias por puntos (MTC)
URL_LICENCIA = "https://slcp.mtc.gob.pe/"
_client_lic = OpenAI()

_capmonster_client: CapMonsterClient | None = None
_capmonster_api_key: str | None = None

# ======================
# Sesiones (modo manual)
# ======================

LICENCIA_SESSION_TTL_SEC = int(os.getenv("LICENCIA_SESSION_TTL_SEC", "120"))
LICENCIA_SESSION_MAX = int(os.getenv("LICENCIA_SESSION_MAX", "50"))


@dataclass
class _LicenciaSession:
    context: object
    page: object
    created_at: float
    kind: str  # "dni" | "nombre"
    params: dict
    captcha_b64: str


_licencia_sessions: dict[str, _LicenciaSession] = {}


async def _close_licencia_session(session_id: str):
    sess = _licencia_sessions.pop(session_id, None)
    if not sess:
        return
    try:
        await sess.context.close()
    except Exception:
        pass


async def _cleanup_licencia_sessions():
    if not _licencia_sessions:
        return

    now = monotonic()
    expired = [
        sid
        for sid, sess in list(_licencia_sessions.items())
        if (now - sess.created_at) > LICENCIA_SESSION_TTL_SEC
    ]
    for sid in expired:
        await _close_licencia_session(sid)

    # Si hay demasiadas sesiones abiertas, cerramos las más antiguas.
    if len(_licencia_sessions) > LICENCIA_SESSION_MAX:
        ordered = sorted(_licencia_sessions.items(), key=lambda kv: kv[1].created_at)
        excess = len(_licencia_sessions) - LICENCIA_SESSION_MAX
        for sid, _sess in ordered[:excess]:
            await _close_licencia_session(sid)


def _new_session_id() -> str:
    return uuid.uuid4().hex


def _captcha_response_payload(session_id: str) -> dict:
    sess = _licencia_sessions.get(session_id)
    if not sess:
        return {}
    return {
        "session_id": session_id,
        "captcha_png_base64": sess.captcha_b64,
        "captcha_data_url": f"data:image/png;base64,{sess.captcha_b64}",
        "captcha_url": f"/licencia-captcha/{session_id}",
        "expires_in_sec": LICENCIA_SESSION_TTL_SEC,
    }


def _get_capmonster_client() -> CapMonsterClient | None:
    global _capmonster_client, _capmonster_api_key

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


def _is_slcp_post_response(resp) -> bool:
    try:
        return resp.request.method == "POST" and resp.url.startswith(URL_LICENCIA)
    except Exception:
        return False


async def _seleccionar_busqueda_por_nombres(page):
    radio = page.locator("#rbtnlBuqueda_2")
    if await radio.count():
        await radio.check()
        # El radio dispara un __doPostBack que recarga paneles
        try:
            await page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass
        await page.wait_for_timeout(300)


async def _seleccionar_busqueda_por_dni(page):
    radio = page.locator("#rbtnlBuqueda_0")
    if await radio.count():
        await radio.check()
        try:
            await page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass
        await page.wait_for_timeout(300)


async def _get_captcha_base64(page) -> str:
    img = page.locator("#imgCaptcha")
    if not await img.count():
        img = page.locator("img[src*='Captcha']")
    if not await img.count():
        raise HTTPException(status_code=500, detail="Licencia: no se encontró imagen de captcha")

    # Reintenta si el elemento se desmonta después del postback
    last_err = None
    for _ in range(3):
        try:
            await img.first.wait_for(state="visible", timeout=2000)
            # Obtiene la imagen EXACTA que el navegador ya cargó, sin disparar un nuevo GET
            # (evita desincronizar el captcha de la sesión).
            try:
                data_url = await img.first.evaluate(
                    """(el) => {
                        try {
                            if (!el || !el.complete || !el.naturalWidth) return null;
                            const canvas = document.createElement('canvas');
                            canvas.width = el.naturalWidth || el.width;
                            canvas.height = el.naturalHeight || el.height;
                            const ctx = canvas.getContext('2d');
                            ctx.drawImage(el, 0, 0);
                            return canvas.toDataURL('image/png');
                        } catch (e) { return null; }
                    }"""
                )
                if data_url and isinstance(data_url, str) and "base64," in data_url:
                    return data_url.split("base64,", 1)[1]
            except Exception:
                pass

            # Fallback: screenshot del elemento (PNG)
            raw_png = await img.first.screenshot(type="png")
            if raw_png:
                return base64.b64encode(raw_png).decode("utf-8")
        except Exception as e:
            last_err = e
            await page.wait_for_timeout(500)
            # reubicar el locator por si se recreó el captcha
            img = page.locator("#imgCaptcha")
            if not await img.count():
                img = page.locator("img[src*='Captcha']")

    raise HTTPException(
        status_code=500, detail=f"Licencia: fallo al capturar captcha ({last_err})"
    )


async def _extract_table(page, selector: str) -> list[dict]:
    """
    Extrae una tabla HTML y la convierte en lista de dict usando el header.
    Ignora columnas sin título.
    """
    try:
        rows = await page.evaluate(
            """(sel) => {
                const t = document.querySelector(sel);
                if (!t) return null;
                return Array.from(t.querySelectorAll('tr')).map(tr =>
                    Array.from(tr.children).map(td => td.innerText.trim())
                );
            }""",
            selector,
        )
    except Exception:
        return []

    if not rows or len(rows) < 2:
        return []

    headers = rows[0]
    cols = [(i, h) for i, h in enumerate(headers) if h and h != "\xa0"]
    data = []
    for r in rows[1:]:
        entry = {}
        for idx, h in cols:
            if idx < len(r):
                entry[h] = r[idx]
        if entry:
            data.append(entry)
    return data


def _parse_resumen(body_text: str) -> dict:
    """
    Extrae campos principales del bloque superior (administrado, licencia, fechas).
    """
    import re

    patterns = {
        "administrado": r"CONSULTA DEL ADMINISTRADO:\s*([^\n]+)",
        "dni": r"NRO\. DE DOCUMENTO DE IDENTIDAD:\s*([^\n]+)",
        "licencia": r"NRO\. DE LICENCIA:\s*([^\n]+)",
        "clase_categoria": r"CLASE Y CATEGORIA:\s*([^\n]+)",
        "vigente_hasta": r"VIGENTE HASTA:\s*([0-9/]+)",
        "estado_licencia": r"ESTADO DE LA LICENCIA:\s*([^\n]+)",
        "faltas": r"FALTAS\s*:\s*([0-9]+)",
        "muy_graves": r"MUY GRAVE\(S\):\s*([0-9]+)",
        "graves": r"GRAVE\(S\):\s*([0-9]+)",
        "puntos_firmes": r"PUNTOS FIRMES ACUMULADOS SON:\s*([0-9]+)",
    }
    out = {}
    for k, pat in patterns.items():
        m = re.search(pat, body_text, flags=re.IGNORECASE)
        if m:
            out[k] = m.group(1).strip()
    # Si no logramos ninguno, devolvemos además líneas con :
    if not out:
        lines = [
            ln.strip()
            for ln in body_text.splitlines()
            if ":" in ln and len(ln.strip()) < 120
        ]
        if lines:
            out["lineas"] = lines
    return out


def _tiene_resumen(resumen: dict) -> bool:
    if not isinstance(resumen, dict) or not resumen:
        return False
    for key in ("administrado", "dni", "licencia", "estado_licencia", "vigente_hasta"):
        val = (resumen.get(key) or "").strip() if isinstance(resumen.get(key), str) else resumen.get(key)
        if val:
            return True
    return False


async def _refresh_captcha(page):
    """
    Intenta refrescar el captcha dando click al botón si existe.
    """
    try:
        btn = page.locator("#btnCaptcha")
        if await btn.count():
            try:
                async with page.expect_response(_is_slcp_post_response, timeout=8000):
                    await btn.click()
            except Exception:
                await btn.click()
            # Dale un momento al navegador para re-renderizar la imagen
            await page.wait_for_timeout(350)
    except Exception:
        pass


async def _cerrar_modal(page):
    """
    Cierra el modal informativo que a veces bloquea el click (#ModalMensaje).
    """
    try:
        modal = page.locator("#ModalMensaje.show, #ModalMensaje.in")
        if await modal.count():
            btn = modal.locator("button[data-dismiss='modal'], .btn-default, button:has-text('Aceptar')")
            if await btn.count():
                await btn.first.click()
                await page.wait_for_timeout(500)
    except Exception:
        pass


async def _forzar_cierre_modal(page):
    """
    Fuerza el cierre del modal y backdrop via JS si sigue bloqueando.
    """
    try:
        await page.evaluate(
            """() => {
                const m = document.querySelector('#ModalMensaje');
                if (m) {
                    m.style.display = 'none';
                    m.classList.remove('show', 'in');
                }
                document.querySelectorAll('.modal-backdrop').forEach(b => b.remove());
            }"""
        )
    except Exception:
        pass


async def _leer_modal(page) -> str:
    """
    Devuelve el texto del modal (#ModalMensaje) si está visible, o ''.
    """
    try:
        modal = page.locator("#ModalMensaje")
        if not await modal.count():
            return ""
        if not await modal.first.is_visible():
            return ""
        txt = (await modal.first.inner_text()).strip()
        return txt
    except Exception:
        return ""


async def _click_buscar(page):
    """
    Click al botón Buscar y espera el POST de ASP.NET (mejor que llamar __doPostBack desde evaluate).
    """
    btn = page.locator("#ibtnBusqNroDoc")
    if not await btn.count():
        raise HTTPException(status_code=500, detail="Licencia: no se encontró botón Buscar")

    await _cerrar_modal(page)
    await _forzar_cierre_modal(page)

    try:
        async with page.expect_response(_is_slcp_post_response, timeout=8000):
            await btn.first.click()
        return
    except Exception:
        pass

    # Fallback: click vía JS (dispara el handler sin meter __doPostBack en contexto strict)
    try:
        async with page.expect_response(_is_slcp_post_response, timeout=8000):
            await btn.first.evaluate("el => el.click()")
        return
    except Exception:
        pass

    # Último recurso: click forzado + espera corta
    await btn.first.click(force=True)
    try:
        await page.wait_for_load_state("networkidle", timeout=6000)
    except Exception:
        pass


def _clean_6_digits(text: str) -> str:
    digits = re.sub(r"[^0-9]", "", text or "")
    return digits[:6]


def _otsu_threshold(gray_img: Image.Image) -> int:
    """
    Calcula el umbral de Otsu (0-255) para binarización automática.
    """
    hist = gray_img.histogram()
    total = sum(hist)
    if total <= 0:
        return 160

    sum_total = 0
    for i, h in enumerate(hist):
        sum_total += i * h

    sum_b = 0
    w_b = 0
    max_var = -1.0
    threshold = 160
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b = sum_b / w_b
        m_f = (sum_total - sum_b) / w_f
        var_between = w_b * w_f * (m_b - m_f) ** 2
        if var_between > max_var:
            max_var = var_between
            threshold = t

    # Clampeo suave para evitar extremos raros
    return max(90, min(210, int(threshold)))


def _prepare_captcha_for_ocr(captcha_b64: str, mode: str = "original") -> bytes:
    """
    Decodifica captcha base64 y retorna bytes PNG listos para OCR.
    mode:
      - original: tal cual
      - gray: escala + limpia (mejor para OCR)
      - bin: binariza y escala (a veces mejor para OCR)
    """
    raw_png = base64.b64decode(captcha_b64)
    if mode == "original":
        return raw_png

    img = Image.open(io.BytesIO(raw_png)).convert("L")
    img = ImageOps.autocontrast(img)
    # Upscale + filtro para reducir ruido (mejora OCR de dígitos)
    img = img.resize((img.width * 2, img.height * 2), resample=Image.BICUBIC)
    img = img.filter(ImageFilter.MedianFilter(size=3))
    if mode == "bin":
        thr = _otsu_threshold(img)
        img = img.point(lambda p, t=thr: 255 if p > t else 0)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


async def _solve_captcha_with_capmonster(captcha_b64: str) -> str | None:
    """
    Resuelve captcha numérico del MTC usando CapMonster ImageToText.
    Devuelve 6 dígitos o None si no fue posible.
    """
    client = _get_capmonster_client()
    if not client:
        return None

    # Orden importa: muchas veces el captcha es más legible en escala de grises reescalada.
    # No conviene demorar mucho entre captura y submit porque el captcha puede expirar.
    for mode in ("gray",):
        try:
            img_bytes = _prepare_captcha_for_ocr(captcha_b64, mode=mode)
            req = ImageToTextRequest(
                image_bytes=img_bytes,
                module_name="universal",
                numeric=1,
                case=False,
                math=False,
            )
            solution = await asyncio.wait_for(client.solve_captcha(req), timeout=6)
            raw = (
                (solution or {}).get("text")
                or (solution or {}).get("answer")
                or (solution or {}).get("code")
                or ""
            )
            digits = _clean_6_digits(str(raw))
            if len(digits) == 6:
                return digits
        except Exception:
            continue

    return None


async def _solve_captcha_strong(captcha_b64: str) -> str:
    """
    Usa un modelo más fuerte para mejorar acierto de captcha.
    """
    model = os.getenv("LICENCIA_CAPTCHA_OPENAI_MODEL", "gpt-4.1")

    def _call(img_bytes: bytes) -> str:
        data_url = "data:image/png;base64," + base64.b64encode(img_bytes).decode("utf-8")
        resp = _client_lic.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Lee el captcha. Responde SOLO con los 6 dígitos exactos (0-9).",
                        },
                        {"type": "input_image", "image_url": data_url},
                    ],
                }
            ],
            instructions="Devuelve únicamente 6 dígitos. Si no puedes leerlos con seguridad, responde vacío.",
        )
        return resp.output_text or ""

    # Variante 1: la misma preparación que usamos para CapMonster (suele limpiar ruido)
    processed = _prepare_captcha_for_ocr(captcha_b64, mode="gray")
    # Variante 2: original (a veces el pre-procesado empeora)
    original = base64.b64decode(captcha_b64)

    for img_bytes in (processed, original):
        try:
            raw = await asyncio.wait_for(asyncio.to_thread(_call, img_bytes), timeout=15)
        except Exception:
            continue
        cleaned = _clean_6_digits(raw)
        if len(cleaned) == 6:
            return cleaned

    return ""


async def _parse_resumen_dom(page) -> dict:
    """
    Intenta leer directamente los labels del resumen si existen en el DOM.
    """
    ids = {
        "administrado": "#lblAdministrado",
        "dni": "#lblDni",
        "licencia": "#lblLicencia",
        "clase_categoria": "#lblClaseCategoria",
        "vigente_hasta": "#lblVigencia",
        "estado_licencia": "#lblEstadoLicencia",
        "muy_graves": "#lblMuyGraves",
        "graves": "#lblGraves",
        "puntos_firmes": "#lblPtsAcumulados",
        "infracciones_acumuladas": "#lblInfAcumuladas",
    }
    out = {}
    for key, sel in ids.items():
        try:
            loc = page.locator(sel)
            if await loc.count():
                txt = (await loc.first.inner_text()).strip()
                if txt:
                    out[key] = txt
        except Exception:
            continue
    return out


def _texto_contiene_error_captcha(texto: str) -> bool:
    texto_lower = (texto or "").lower()
    if not texto_lower:
        return False
    for msg in [
        "captcha incorrecto",
        "código de seguridad incorrecto",
        "codigo de seguridad incorrecto",
        "captcha inválido",
        "captcha invalido",
        "ingresar correctamente el captcha",
        "ingrese el captcha",
        "ingrese el código captcha",
        "ingrese el codigo captcha",
        "ingresar captcha",
        "no coincide con la imagen",
        "token captcha invalido",
    ]:
        if msg in texto_lower:
            return True
    return False


async def _parse_resultado_licencia(page) -> dict:
    """
    Normaliza extracción de resultado (resumen/tablas) y flags.
    """
    try:
        tabla_tramites = await _extract_table(page, "#gbtramite")
    except Exception:
        tabla_tramites = []
    try:
        tabla_bonif = await _extract_table(page, "#gvBonificacion")
    except Exception:
        tabla_bonif = []

    await page.wait_for_timeout(350)
    body_text = await page.inner_text("body")
    mensaje_modal = await _leer_modal(page)

    texto_lower = body_text.lower()
    no_result = "no se encontraron" in texto_lower
    sin_info_registro = (
        "no se encontró información en el registro nacional de sanciones" in texto_lower
    )

    resumen_dom = await _parse_resumen_dom(page)
    resumen = resumen_dom or _parse_resumen(body_text)
    tiene_resumen = _tiene_resumen(resumen)

    captcha_valido = True
    if _texto_contiene_error_captcha(body_text) or _texto_contiene_error_captcha(mensaje_modal):
        captcha_valido = False

    # Si encontramos datos, el captcha fue válido aunque existan frases genéricas en el body
    if tiene_resumen:
        no_result = False
        captcha_valido = True

    # Mensaje explícito: no hay info en el registro, pero captcha sí fue válido
    if sin_info_registro:
        no_result = True
        captcha_valido = True

    # Si no hay resumen ni tablas, normalmente es submit fallido/captcha inválido
    if not sin_info_registro and not tiene_resumen and not tabla_tramites and not tabla_bonif:
        captcha_valido = False

    return {
        "captcha_valido": captcha_valido,
        "tabla_tramites": tabla_tramites,
        "tabla_bonif": tabla_bonif,
        "resumen": resumen,
        "no_result": no_result,
        "mensaje_modal": mensaje_modal,
        "body_text": body_text,
    }


async def _submit_captcha_y_parse(page, captcha_text: str) -> dict:
    captcha_input = page.locator("#txtCaptcha")
    if not await captcha_input.count():
        raise HTTPException(status_code=500, detail="Licencia: falta input de captcha")

    await captcha_input.fill(captcha_text)

    await _click_buscar(page)
    try:
        await page.wait_for_selector(
            "#pnlAdministrado, #lblAdministrado, #ModalMensaje.show, #ModalMensaje.in, text=CONSULTA DEL ADMINISTRADO",
            timeout=6000,
        )
    except Exception:
        pass

    return await _parse_resultado_licencia(page)


async def iniciar_sesion_licencia_dni(dni: str, browser) -> dict:
    """
    Inicia una sesión Playwright para consulta por DNI y devuelve el captcha como PNG.

    Flujo recomendado (Postman/UI):
      1) POST /consulta-licencia-dni-init -> devuelve session_id + captcha_png_base64
      2) Usuario resuelve captcha y envía:
         POST /consulta-licencia-dni-submit {session_id, captcha_text}
    """
    await _cleanup_licencia_sessions()

    context = await browser.new_context(locale="es-PE")
    page = await context.new_page()
    try:
        await page.goto(URL_LICENCIA, wait_until="domcontentloaded")
        await page.wait_for_timeout(800)

        await _seleccionar_busqueda_por_dni(page)
        await _cerrar_modal(page)

        # Asegurar tipo documento = DNI (value=2)
        try:
            tipo_doc = page.locator("#ddlTipoDocumento")
            if await tipo_doc.count():
                cur = ""
                try:
                    cur = (await tipo_doc.input_value()) or ""
                except Exception:
                    cur = ""
                if cur != "2":
                    try:
                        async with page.expect_response(_is_slcp_post_response, timeout=12000):
                            await tipo_doc.select_option(value="2")
                    except Exception:
                        await tipo_doc.select_option(value="2")
                        try:
                            await page.wait_for_load_state("networkidle", timeout=6000)
                        except Exception:
                            pass
                    await page.wait_for_timeout(350)
        except Exception:
            pass

        inp_dni = page.locator("#txtNroDocumento")
        if not await inp_dni.count():
            raise HTTPException(status_code=500, detail="Licencia: falta input de N° documento")
        await inp_dni.fill(dni.strip())

        captcha_b64 = await _get_captcha_base64(page)
        session_id = _new_session_id()
        _licencia_sessions[session_id] = _LicenciaSession(
            context=context,
            page=page,
            created_at=monotonic(),
            kind="dni",
            params={"dni": dni.strip()},
            captcha_b64=captcha_b64,
        )
        return {"ok": True, "tipo": "dni", "dni": dni.strip(), **_captcha_response_payload(session_id)}
    except Exception:
        await context.close()
        raise


async def iniciar_sesion_licencia_nombre(ap_paterno: str, ap_materno: str, nombre: str, browser) -> dict:
    """
    Inicia sesión para búsqueda por apellidos y nombres, devolviendo captcha PNG.
    """
    await _cleanup_licencia_sessions()

    context = await browser.new_context(locale="es-PE")
    page = await context.new_page()
    try:
        await page.goto(URL_LICENCIA, wait_until="domcontentloaded")
        await page.wait_for_timeout(800)

        await _seleccionar_busqueda_por_nombres(page)
        await _cerrar_modal(page)

        inp_ape_pat = page.locator("#txtApePaterno")
        inp_ape_mat = page.locator("#txtApeMaterno")
        inp_nombre = page.locator("#txtNombre")

        for loc, label in [
            (inp_ape_pat, "apellido paterno"),
            (inp_ape_mat, "apellido materno"),
            (inp_nombre, "nombre(s)"),
        ]:
            if not await loc.count():
                raise HTTPException(status_code=500, detail=f"Licencia: falta input de {label}")

        ap_paterno_u = ap_paterno.strip().upper()
        ap_materno_u = ap_materno.strip().upper()
        nombre_u = nombre.strip().upper()

        await inp_ape_pat.fill(ap_paterno_u)
        await inp_ape_mat.fill(ap_materno_u)
        await inp_nombre.fill(nombre_u)

        captcha_b64 = await _get_captcha_base64(page)
        session_id = _new_session_id()
        _licencia_sessions[session_id] = _LicenciaSession(
            context=context,
            page=page,
            created_at=monotonic(),
            kind="nombre",
            params={
                "ap_paterno": ap_paterno_u,
                "ap_materno": ap_materno_u,
                "nombre": nombre_u,
            },
            captcha_b64=captcha_b64,
        )
        return {
            "ok": True,
            "tipo": "nombre",
            "ap_paterno": ap_paterno_u,
            "ap_materno": ap_materno_u,
            "nombre": nombre_u,
            **_captcha_response_payload(session_id),
        }
    except Exception:
        await context.close()
        raise


async def enviar_captcha_sesion_licencia(session_id: str, captcha_text: str) -> dict:
    """
    Envía el captcha (resuelto por el usuario) para una sesión existente.

    Si el captcha es inválido, refresca la imagen y devuelve need_captcha=true.
    """
    await _cleanup_licencia_sessions()

    sess = _licencia_sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Licencia: sesión expirada o no existe")

    digits = _clean_6_digits(captcha_text)
    if len(digits) != 6:
        raise HTTPException(status_code=400, detail="Licencia: captcha_text debe tener 6 dígitos")

    # Extiende TTL por actividad
    sess.created_at = monotonic()

    page = sess.page
    resultado = await _submit_captcha_y_parse(page, digits)

    if not resultado["captcha_valido"]:
        # Intenta refrescar el captcha y mantener la sesión viva para reintentar
        await _refresh_captcha(page)
        await page.wait_for_timeout(350)
        try:
            sess.captcha_b64 = await _get_captcha_base64(page)
        except Exception:
            sess.captcha_b64 = sess.captcha_b64
        sess.created_at = monotonic()

        return {
            "ok": False,
            "captcha_valido": False,
            "need_captcha": True,
            "mensaje_modal": resultado.get("mensaje_modal") or "Captcha inválido",
            **_captcha_response_payload(session_id),
        }

    # Éxito: cerramos sesión y devolvemos resultado en formato similar a endpoints actuales
    params = sess.params or {}
    await _close_licencia_session(session_id)

    base = {
        "ok": True,
        "captcha_ingresado": digits,
        "captcha_valido": True,
        "tabla_tramites": resultado["tabla_tramites"],
        "tabla_bonificacion": resultado["tabla_bonif"],
        "resumen": resultado["resumen"],
        "sin_resultados": resultado["no_result"],
        "resultado_crudo": resultado["body_text"],
    }
    if sess.kind == "dni":
        base["dni"] = params.get("dni")
    else:
        base["ap_paterno"] = params.get("ap_paterno")
        base["ap_materno"] = params.get("ap_materno")
        base["nombre"] = params.get("nombre")

    return base


def get_captcha_b64_sesion_licencia(session_id: str) -> str:
    sess = _licencia_sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Licencia: sesión expirada o no existe")
    return sess.captcha_b64


async def consulta_licencia_por_nombre(ap_paterno: str, ap_materno: str, nombre: str, browser):
    """
    Busca licencias por apellidos y nombre completo en https://slcp.mtc.gob.pe/.
    """
    context = await browser.new_context(locale="es-PE")
    page = await context.new_page()

    await page.goto(URL_LICENCIA, wait_until="domcontentloaded")
    await page.wait_for_timeout(800)

    # Cambiar a modo de búsqueda por nombre completo
    await _seleccionar_busqueda_por_nombres(page)
    await _cerrar_modal(page)

    # Inputs de nombres
    inp_ape_pat = page.locator("#txtApePaterno")
    inp_ape_mat = page.locator("#txtApeMaterno")
    inp_nombre = page.locator("#txtNombre")

    for loc, label in [
        (inp_ape_pat, "apellido paterno"),
        (inp_ape_mat, "apellido materno"),
        (inp_nombre, "nombre(s)"),
    ]:
        if not await loc.count():
            await context.close()
            raise HTTPException(status_code=500, detail=f"Licencia: falta input de {label}")

    await inp_ape_pat.fill(ap_paterno.strip().upper())
    await inp_ape_mat.fill(ap_materno.strip().upper())
    await inp_nombre.fill(nombre.strip().upper())

    async def _intentar_consulta(prefer_solver: str):
        captcha_text = ""
        captcha_solver = prefer_solver

        captcha_b64 = await _get_captcha_base64(page)
        if prefer_solver == "openai":
            captcha_text = _clean_6_digits(await _solve_captcha_strong(captcha_b64))
        else:
            tmp = await _solve_captcha_with_capmonster(captcha_b64)
            if tmp and len(tmp) == 6:
                captcha_text = tmp
            else:
                # Si CapMonster no devuelve 6 dígitos, fallback inmediato
                captcha_solver = "openai"
                captcha_text = _clean_6_digits(await _solve_captcha_strong(captcha_b64))

        # Si no tenemos 6 dígitos, evitamos enviar el formulario (solo gastaría intento)
        if len(captcha_text) != 6:
            return {
                "captcha_text": captcha_text,
                "captcha_solver": captcha_solver,
                "captcha_valido": False,
                "tabla_tramites": [],
                "tabla_bonif": [],
                "resumen": {},
                "no_result": False,
                "mensaje_modal": "",
                "body_text": "",
            }

        captcha_input = page.locator("#txtCaptcha")
        if not await captcha_input.count():
            raise HTTPException(status_code=500, detail="Licencia: falta input de captcha")

        await captcha_input.fill(captcha_text)

        await _click_buscar(page)
        try:
            await page.wait_for_selector(
                "#pnlAdministrado, #lblAdministrado, #ModalMensaje.show, #ModalMensaje.in, text=CONSULTA DEL ADMINISTRADO",
                timeout=6000,
            )
        except Exception:
            pass
        mensaje_modal = await _leer_modal(page)
        try:
            tabla_tramites = await _extract_table(page, "#gbtramite")
        except Exception:
            tabla_tramites = []
        try:
            tabla_bonif = await _extract_table(page, "#gvBonificacion")
        except Exception:
            tabla_bonif = []

        # Espera breve para que el DOM se actualice
        await page.wait_for_timeout(350)
        body_text = await page.inner_text("body")

        texto_lower = body_text.lower()
        no_result = "no se encontraron" in texto_lower

        captcha_valido = True
        for msg in [
            "captcha incorrecto",
            "código de seguridad incorrecto",
            "ingrese el captcha",
            "ingrese el código captcha",
            "ingrese el codigo captcha",
            "ingresar captcha",
            "no coincide con la imagen",
        ]:
            if msg in texto_lower:
                captcha_valido = False
                break
        if mensaje_modal:
            mod_lower = mensaje_modal.lower()
            if "captcha" in mod_lower and (
                "incorrect" in mod_lower
                or "inval" in mod_lower
                or "no coincide" in mod_lower
            ):
                captcha_valido = False
            if "ingrese" in mod_lower and "captcha" in mod_lower:
                captcha_valido = False
            if "ingrese" in mod_lower and "documento" in mod_lower:
                captcha_valido = False

        resumen_dom = await _parse_resumen_dom(page)
        resumen = resumen_dom or _parse_resumen(body_text)
        tiene_resumen = _tiene_resumen(resumen)
        if tiene_resumen:
            no_result = False
            captcha_valido = True

        sin_info_registro = "no se encontró información en el registro nacional de sanciones" in texto_lower
        if sin_info_registro:
            no_result = True
            captcha_valido = True
        # Si no detectamos datos y tampoco hay tablas, asumimos captcha malo
        if not sin_info_registro and not tiene_resumen and not tabla_tramites and not tabla_bonif:
            captcha_valido = False

        return {
            "captcha_text": captcha_text,
            "captcha_solver": captcha_solver,
            "captcha_valido": captcha_valido,
            "tabla_tramites": tabla_tramites,
            "tabla_bonif": tabla_bonif,
            "resumen": resumen,
            "no_result": no_result,
            "mensaje_modal": mensaje_modal,
            "body_text": body_text,
        }

    resultado = None
    solver_pref = (os.getenv("LICENCIA_CAPTCHA_SOLVER") or "openai").strip().lower()
    # OpenAI es el más efectivo para este captcha; CapMonster queda como opción si quieres evitar OpenAI.
    if solver_pref == "capmonster":
        preferencias = ["capmonster", "capmonster", "openai"]
    else:
        preferencias = ["openai", "openai"]
    for i, prefer in enumerate(preferencias):
        resultado = await _intentar_consulta(prefer)
        if resultado["captcha_valido"]:
            break
        await _refresh_captcha(page)
        await page.wait_for_timeout(600)

    await context.close()

    return {
        "ok": True,
        "ap_paterno": ap_paterno,
        "ap_materno": ap_materno,
        "nombre": nombre,
        "captcha_detectado": resultado["captcha_text"],
        "captcha_solver": resultado.get("captcha_solver"),
        "captcha_valido": resultado["captcha_valido"],
        "tabla_tramites": resultado["tabla_tramites"],
        "tabla_bonificacion": resultado["tabla_bonif"],
        "resumen": resultado["resumen"],
        "sin_resultados": resultado["no_result"],
        "resultado_crudo": resultado["body_text"],
    }


async def consulta_licencia_por_dni(dni: str, browser):
    """
    Busca licencias por número de documento (DNI) en https://slcp.mtc.gob.pe/.
    """
    context = await browser.new_context(locale="es-PE")
    page = await context.new_page()

    await page.goto(URL_LICENCIA, wait_until="domcontentloaded")
    await page.wait_for_timeout(800)

    await _seleccionar_busqueda_por_dni(page)
    await _cerrar_modal(page)

    # Asegurar tipo documento = DNI (value=2)
    try:
        tipo_doc = page.locator("#ddlTipoDocumento")
        if await tipo_doc.count():
            cur = ""
            try:
                cur = (await tipo_doc.input_value()) or ""
            except Exception:
                cur = ""
            if cur != "2":
                try:
                    async with page.expect_response(_is_slcp_post_response, timeout=12000):
                        await tipo_doc.select_option(value="2")
                except Exception:
                    await tipo_doc.select_option(value="2")
                    try:
                        await page.wait_for_load_state("networkidle", timeout=6000)
                    except Exception:
                        pass
                await page.wait_for_timeout(350)
    except Exception:
        pass

    inp_dni = page.locator("#txtNroDocumento")
    if not await inp_dni.count():
        await context.close()
        raise HTTPException(status_code=500, detail="Licencia: falta input de N° documento")

    await inp_dni.fill(dni.strip())

    async def _intentar_consulta(prefer_solver: str):
        captcha_text = ""
        captcha_solver = prefer_solver

        captcha_b64 = await _get_captcha_base64(page)
        if prefer_solver == "openai":
            captcha_text = _clean_6_digits(await _solve_captcha_strong(captcha_b64))
        else:
            captcha_text = (await _solve_captcha_with_capmonster(captcha_b64)) or ""

        if len(captcha_text) != 6:
            return {
                "captcha_text": captcha_text,
                "captcha_solver": captcha_solver,
                "captcha_valido": False,
                "tabla_tramites": [],
                "tabla_bonif": [],
                "resumen": {},
                "no_result": False,
                "mensaje_modal": "",
                "body_text": "",
            }

        captcha_input = page.locator("#txtCaptcha")
        if not await captcha_input.count():
            raise HTTPException(status_code=500, detail="Licencia: falta input de captcha")

        await captcha_input.fill(captcha_text)
        await _click_buscar(page)
        try:
            await page.wait_for_selector(
                "#pnlAdministrado, #lblAdministrado, #ModalMensaje.show, #ModalMensaje.in, text=CONSULTA DEL ADMINISTRADO",
                timeout=6000,
            )
        except Exception:
            pass
        mensaje_modal = await _leer_modal(page)

        try:
            tabla_tramites = await _extract_table(page, "#gbtramite")
        except Exception:
            tabla_tramites = []
        try:
            tabla_bonif = await _extract_table(page, "#gvBonificacion")
        except Exception:
            tabla_bonif = []

        # Espera breve para que el DOM se actualice
        await page.wait_for_timeout(350)
        body_text = await page.inner_text("body")

        texto_lower = body_text.lower()
        no_result = "no se encontraron" in texto_lower

        # Detectar errores de captcha (evita etiquetas genéricas)
        captcha_valido = True
        for msg in [
            "captcha incorrecto",
            "código de seguridad incorrecto",
            "codigo de seguridad incorrecto",
            "captcha inválido",
            "captcha invalido",
            "ingresar correctamente el captcha",
            "ingrese el código captcha",
            "ingrese el codigo captcha",
            "no coincide con la imagen",
        ]:
            if msg in texto_lower:
                captcha_valido = False
                break
        if mensaje_modal:
            mod_lower = mensaje_modal.lower()
            if "captcha" in mod_lower and (
                "incorrect" in mod_lower
                or "inval" in mod_lower
                or "no coincide" in mod_lower
            ):
                captcha_valido = False
            if "ingrese" in mod_lower and "captcha" in mod_lower:
                captcha_valido = False
            if "ingrese" in mod_lower and "documento" in mod_lower:
                captcha_valido = False

        resumen_dom = await _parse_resumen_dom(page)
        resumen = resumen_dom or _parse_resumen(body_text)

        tiene_resumen = _tiene_resumen(resumen)
        if tiene_resumen:
            no_result = False
            captcha_valido = True

        # Mensaje explícito: no hay info en el registro, pero captcha es válido
        sin_info_registro = "no se encontró información en el registro nacional de sanciones" in texto_lower
        if sin_info_registro:
            no_result = True
            captcha_valido = True

        # Si no hay resumen ni tablas, o la página parece no haber aceptado el submit, forzamos reintento
        if (
            not sin_info_registro
            and not tiene_resumen
            and not tabla_tramites
            and not tabla_bonif
            and "ingrese número de documento" in texto_lower
        ):
            captcha_valido = False
        if not sin_info_registro and not tiene_resumen and not tabla_tramites and not tabla_bonif:
            captcha_valido = False

        return {
            "captcha_text": captcha_text,
            "captcha_solver": captcha_solver,
            "captcha_valido": captcha_valido,
            "tabla_tramites": tabla_tramites,
            "tabla_bonif": tabla_bonif,
            "resumen": resumen,
            "no_result": no_result,
            "mensaje_modal": mensaje_modal,
            "body_text": body_text,
        }

    solver_pref = (os.getenv("LICENCIA_CAPTCHA_SOLVER") or "openai").strip().lower()
    if solver_pref == "capmonster":
        preferencias = ["capmonster", "capmonster", "openai"]
    else:
        preferencias = ["openai", "openai"]

    resultado = None
    for prefer in preferencias:
        resultado = await _intentar_consulta(prefer)
        if resultado["captcha_valido"]:
            break
        await _refresh_captcha(page)
        await page.wait_for_timeout(600)

    await context.close()

    return {
        "ok": True,
        "dni": dni,
        "captcha_detectado": resultado["captcha_text"],
        "captcha_solver": resultado.get("captcha_solver"),
        "captcha_valido": resultado["captcha_valido"],
        "tabla_tramites": resultado["tabla_tramites"],
        "tabla_bonificacion": resultado["tabla_bonif"],
        "resumen": resultado["resumen"],
        "sin_resultados": resultado["no_result"],
        "resultado_crudo": resultado["body_text"],
    }
