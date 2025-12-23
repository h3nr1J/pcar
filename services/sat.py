import os
import io
import base64

from fastapi import HTTPException
from PIL import Image

from services.sunarp import solve_captcha_with_openai

# Página específica de captura de vehículos (versión actual)
# Se puede sobreescribir con URL_SAT_CAPTURA en .env si cambia el path.
URL_SAT = os.getenv(
    "URL_SAT_CAPTURA",
    "https://www.sat.gob.pe/VirtualSAT/modulos/Capturas.aspx?tri=C",
)


async def _get_plate_input(page):
    """
    Devuelve el locator del input de placa del SAT.
    Prueba: id, name y placeholder.
    """
    # Esperamos a que cargue bien
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(1000)

    # 1) ID típico
    loc = page.locator("#ctl00_cplPrincipal_txtPlaca")
    if await loc.count():
        await loc.first.wait_for(state="visible")
        return loc.first

    # 2) name (ASP.NET)
    loc = page.locator("input[name='ctl00$cplPrincipal$txtPlaca']")
    if await loc.count():
        await loc.first.wait_for(state="visible")
        return loc.first

    # 3) placeholder
    loc = page.locator("input[placeholder='Ingresar Placa']")
    if await loc.count():
        await loc.first.wait_for(state="visible")
        return loc.first

    # 4) algo con 'Placa'
    loc = page.locator("input[placeholder*='Placa' i]")
    if await loc.count():
        await loc.first.wait_for(state="visible")
        return loc.first

    return None


async def _get_captcha_image_base64(page) -> str:
    """
    Captura la imagen del captcha y la devuelve en base64.
    """
    # ID típico en SAT (puede variar un poco, por eso usamos [id*='imgCaptcha'])
    img = page.locator("img[id*='imgCaptcha']")
    if not await img.count():
        # Captchas en Capturas.aspx vienen como ../controles/JpegImage_VB.aspx con class captcha_class
        img = page.locator("img.captcha_class, img[src*='JpegImage_VB']")
    if not await img.count():
        raise HTTPException(status_code=500, detail="SAT: no se encontró imagen de captcha")

    raw_png = await img.screenshot(type="png")
    pil = Image.open(io.BytesIO(raw_png)).convert("L")
    buf = io.BytesIO()
    pil.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


async def _get_captcha_input(page):
    loc = page.locator("#ctl00_cplPrincipal_txtCaptcha")
    if await loc.count():
        return loc.first

    loc = page.locator("input[placeholder*='seguridad' i]")
    if await loc.count():
        return loc.first

    return None


async def _get_buscar_button(page):
    loc = page.locator("#ctl00_cplPrincipal_CaptchaContinue")
    if await loc.count():
        return loc.first

    loc = page.locator("input[type='submit'][value='Buscar']")
    if await loc.count():
        return loc.first

    loc = page.locator("button:has-text('Buscar')")
    if await loc.count():
        return loc.first

    return None


async def consulta_sat(placa: str, browser):
    """
    Consulta captura de vehículos SAT Lima.
    """
    context = await browser.new_context(locale="es-PE")
    page = await context.new_page()

    await page.goto(URL_SAT, wait_until="networkidle")
    print("SAT URL actual:", page.url)  # 👈 te ayuda a ver a dónde está entrando realmente
    await page.wait_for_timeout(1000)

    # 1) Input de placa
    placa = placa.strip().upper()
    placa_input = await _get_plate_input(page)
    if not placa_input:
        await context.close()
        raise HTTPException(
            status_code=500,
            detail="No se encontró input de placa SAT",
        )

    await placa_input.fill(placa)

    # 2) Captcha
    captcha_b64 = await _get_captcha_image_base64(page)
    captcha_text = await solve_captcha_with_openai(captcha_b64)

    captcha_input = await _get_captcha_input(page)
    if not captcha_input:
        await context.close()
        raise HTTPException(
            status_code=500,
            detail="No se encontró input de captcha SAT",
        )

    await captcha_input.fill(captcha_text)

    # 3) Botón Buscar
    btn = await _get_buscar_button(page)
    if not btn:
        await context.close()
        raise HTTPException(
            status_code=500,
            detail="No se encontró botón 'Buscar' SAT",
        )

    await btn.click()
    await page.wait_for_timeout(3000)

    # 4) Resultado
    try:
        body_text = await page.inner_text("body")
    except Exception:
        await context.close()
        raise HTTPException(
            status_code=500,
            detail="SAT: no se pudo leer el resultado",
        )

    await context.close()

    return {
        "placa": placa,
        "captcha_detectado": captcha_text,
        "resultado_crudo": body_text,
    }
