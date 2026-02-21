import base64
import io

from fastapi import HTTPException
from PIL import Image

from services.sunarp import solve_captcha_with_capmonster

URL_SAT_CALLAO = "https://pagopapeletascallao.pe/buscar"


async def _get_captcha_b64(page) -> str:
    """
    La imagen viene como data:image/png;base64 en el src.
    Si no, tomamos screenshot.
    """
    img = page.locator("img[src*='captcha'], img[src^='data:image']")
    if not await img.count():
        raise HTTPException(
            status_code=500,
            detail="SAT Callao: no se encontró imagen de captcha",
        )

    src = await img.first.get_attribute("src")
    if src and src.startswith("data:image"):
        return src.split("base64,", 1)[-1]

    # fallback: screenshot
    raw_png = await img.first.screenshot(type="png")
    pil = Image.open(io.BytesIO(raw_png)).convert("L")
    buf = io.BytesIO()
    pil.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


async def consulta_satcallao(placa: str, browser):
    """
    Consulta de papeletas en pagopapeletascallao.pe.
    """
    context = await browser.new_context(locale="es-PE")
    page = await context.new_page()

    await page.goto(URL_SAT_CALLAO, wait_until="domcontentloaded")
    await page.wait_for_timeout(1000)

    # Tipo de búsqueda: dejar en Número de Placa (value=1)
    try:
        select = page.locator("#tipo_busqueda")
        if await select.count():
            await select.select_option(value="1")
    except Exception:
        pass

    placa = placa.strip().upper()
    inp_placa = page.locator("#valor_busqueda")
    if not await inp_placa.count():
        await context.close()
        raise HTTPException(status_code=500, detail="SAT Callao: no se encontró input de placa")

    await inp_placa.fill(placa)

    # Captcha base64
    captcha_b64 = await _get_captcha_b64(page)
    captcha_text = await solve_captcha_with_capmonster(captcha_b64)

    captcha_input = page.locator("#captcha")
    if not await captcha_input.count():
        await context.close()
        raise HTTPException(status_code=500, detail="SAT Callao: no se encontró input de captcha")

    await captcha_input.fill(captcha_text)

    btn = page.locator("#idBuscar")
    if not await btn.count():
        await context.close()
        raise HTTPException(status_code=500, detail="SAT Callao: no se encontró botón Buscar")

    await btn.click()
    await page.wait_for_timeout(3500)

    try:
        body_text = await page.inner_text("body")
    except Exception:
        await context.close()
        raise HTTPException(status_code=500, detail="SAT Callao: no se pudo leer el resultado")

    await context.close()

    texto_lower = body_text.lower()
    captcha_valido = True
    if "captcha incorrecto" in texto_lower or "código incorrecto" in texto_lower:
        captcha_valido = False

    return {
        "ok": True,
        "placa": placa,
        "captcha_detectado": captcha_text,
        "captcha_valido": captcha_valido,
        "resultado_crudo": body_text,
    }
