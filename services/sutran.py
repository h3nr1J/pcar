import io
import base64

from fastapi import HTTPException
from PIL import Image

from services.sunarp import solve_captcha_with_capmonster

SUTRAN_URL = "https://www.sutran.gob.pe/consultas/record-de-infracciones/record-de-infracciones/"


async def _get_form_frame(page):
    """
    La página de Sutran embebe el formulario en un iframe:
    https://webexterno.sutran.gob.pe/WebExterno/Pages/frmRecordInfracciones.aspx
    """
    for f in page.frames:
        if "frmRecordInfracciones.aspx" in f.url:
            return f
    return None


async def _get_captcha_frame(page):
    for f in page.frames:
        if "Captcha.aspx" in f.url:
            return f
    return None


async def _get_plate_input(frame):
    loc = frame.locator("#txtPlaca")
    if await loc.count():
        return loc.first
    loc = frame.get_by_placeholder("Ingrese Placa Vehicular")
    if await loc.count():
        return loc.first
    return None


async def _get_captcha_input(frame):
    loc = frame.locator("#TxtCodImagen")
    if await loc.count():
        return loc.first
    loc = frame.get_by_placeholder("Ingrese el código aquí")
    if await loc.count():
        return loc.first
    return None


async def _get_captcha_image_base64(page) -> str:
    # El captcha está en un iframe aparte (Captcha.aspx?numAleatorio=...)
    frame = await _get_captcha_frame(page)
    if not frame:
        raise HTTPException(
            status_code=500,
            detail="SUTRAN: no se encontró el iframe del captcha",
        )
    img = frame.locator("img")
    if not await img.count():
        raise HTTPException(
            status_code=500,
            detail="SUTRAN: no se encontró imagen de captcha",
        )
    raw_png = await img.first.screenshot(type="png")
    pil = Image.open(io.BytesIO(raw_png)).convert("L")
    buf = io.BytesIO()
    pil.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


async def _get_buscar_button(frame):
    loc = frame.locator("#BtnBuscar")
    if await loc.count():
        return loc.first
    loc = frame.get_by_role("button", name="Buscar")
    if await loc.count():
        return loc.first
    loc = frame.locator("input[type='submit'][value*='Buscar']")
    if await loc.count():
        return loc.first
    loc = frame.locator("button:has-text('Buscar')")
    if await loc.count():
        return loc.first
    return None


async def consulta_sutran(placa: str, browser):
    """
    Consulta récord de infracciones en Sutran por placa.
    """
    context = await browser.new_context(locale="es-PE")
    page = await context.new_page()

    await page.goto(SUTRAN_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(1200)

    frame = await _get_form_frame(page)
    if not frame:
        await context.close()
        raise HTTPException(status_code=500, detail="SUTRAN: no se encontró el iframe del formulario")

    placa = placa.strip().upper()
    placa_input = await _get_plate_input(frame)
    if not placa_input:
        await context.close()
        raise HTTPException(status_code=500, detail="SUTRAN: no se encontró input de placa")
    await placa_input.fill(placa)

    captcha_b64 = await _get_captcha_image_base64(page)
    captcha_text = await solve_captcha_with_capmonster(captcha_b64)

    captcha_input = await _get_captcha_input(frame)
    if not captcha_input:
        await context.close()
        raise HTTPException(status_code=500, detail="SUTRAN: no se encontró input de captcha")
    await captcha_input.fill(captcha_text)

    btn = await _get_buscar_button(frame)
    if not btn:
        await context.close()
        raise HTTPException(status_code=500, detail="SUTRAN: no se encontró botón Buscar")

    await btn.click()
    await page.wait_for_timeout(4000)

    try:
        body_text = await frame.inner_text("body")
    except Exception:
        try:
            body_text = await page.inner_text("body")
        except Exception:
            await context.close()
            raise HTTPException(status_code=500, detail="SUTRAN: no se pudo leer el resultado")

    await context.close()

    texto_lower = body_text.lower()
    captcha_ok = True
    if "código ingresado es incorrecto" in texto_lower or "codigo ingresado es incorrecto" in texto_lower:
        captcha_ok = False

    return {
        "ok": True,
        "placa": placa,
        "captcha_detectado": captcha_text,
        "captcha_valido": captcha_ok,
        "resultado_crudo": body_text,
    }
