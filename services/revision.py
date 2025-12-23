import os

from fastapi import HTTPException

# Reutilizamos el lector de captchas que ya tienes en SUNARP
from services.sunarp import solve_captcha_with_openai


# URL oficial de consulta CITV (MTC)
URL_CITV = os.getenv("URL_CITV_ITV", "https://rec.mtc.gob.pe/Citv/ArConsultaCitv")


# ================== HELPERS PLAYWRIGHT ==================


async def _get_placa_input(page):
    """
    Input de placa.

    En la página del MTC el input suele ser algo tipo:
      <input type="text" id="txtPlaca" ...>

    Pero dejamos varios fallbacks por si cambian cosas.
    """
    # 1) id más probable
    loc = page.locator("#txtPlaca")
    if await loc.count():
        return loc.first

    # 2) por label "Placa"
    try:
        loc = page.get_by_label("Placa")
        if await loc.count():
            return loc.first
    except Exception:
        pass

    # 3) por placeholder
    loc = page.get_by_placeholder("Placa")
    if await loc.count():
        return loc.first

    # 4) último recurso: primer input de texto dentro del formulario
    loc = page.locator("input[type='text']")
    if await loc.count():
        return loc.first

    return None


async def _get_captcha_input(page):
    """
    Input donde se escribe el captcha:

      <input type="text" name="texCaptcha" id="texCaptcha" ...>
    """
    loc = page.locator("#texCaptcha")
    if await loc.count():
        return loc.first

    loc = page.locator("input[name='texCaptcha']")
    if await loc.count():
        return loc.first

    return None


async def _get_captcha_base64(page) -> str:
    """
    La imagen del captcha ya viene en base64 en el src:

      <img id="imgCaptcha" src="data:image/png;base64,...." />

    Solo extraemos la parte base64.
    """
    img = page.locator("#imgCaptcha")
    if not await img.count():
        raise HTTPException(
            status_code=500,
            detail="CITV: no se encontró la imagen de captcha (imgCaptcha)",
        )

    src = await img.first.get_attribute("src")
    if not src:
        raise HTTPException(
            status_code=500,
            detail="CITV: la imagen de captcha no tiene atributo src",
        )

    # Ej: data:image/png;base64,AAAA...
    if "base64," not in src:
        raise HTTPException(
            status_code=500,
            detail="CITV: formato inesperado en el src del captcha",
        )

    b64 = src.split("base64,", 1)[1].strip()
    return b64


async def _get_buscar_button(page):
    """
    Botón 'Buscar'.

    En la página se ve un botón rojo con texto 'Buscar'.
    Probamos varias estrategias.
    """
    # 1) por rol / nombre accesible
    btn = page.get_by_role("button", name="Buscar")
    if await btn.count():
        return btn.first

    # 2) por texto
    btn = page.locator("button:has-text('Buscar')")
    if await btn.count():
        return btn.first

    # 3) input submit
    btn = page.locator("input[type='submit'][value='Buscar']")
    if await btn.count():
        return btn.first

    return None


# ================== FUNCIÓN PRINCIPAL ==================


async def consulta_revision(placa: str, browser):
    """
    Consulta de Certificados de Inspección Técnica Vehicular (CITV).

    Flujo:
    - Abrir página
    - Asegurar tipo de búsqueda = Placa (si aplica)
    - Rellenar placa
    - Leer captcha (base64 del <img>)
    - Enviar a OpenAI para OCR
    - Rellenar captcha
    - Click en Buscar
    - Leer resultados
    """
    context = await browser.new_context(locale="es-PE")
    page = await context.new_page()

    # 1) Ir a la página de CITV
    await page.goto(URL_CITV, wait_until="networkidle")
    await page.wait_for_timeout(1000)

    placa = placa.strip().upper()

    # 2) (opcional) seleccionar "Placa" en Tipo de Búsqueda
    try:
        # Muchas veces el select ya viene en "Placa"
        select_tipo = page.get_by_label("Tipo de Búsqueda")
        if await select_tipo.count():
            await select_tipo.select_option(label="Placa")
    except Exception:
        # Si no existe ese label, no pasa nada
        pass

    # 3) Input de placa
    placa_input = await _get_placa_input(page)
    if not placa_input:
        await context.close()
        raise HTTPException(
            status_code=500,
            detail="CITV: no se encontró el input de placa",
        )

    await placa_input.fill(placa)

    # 4) Captcha → base64 (desde el src de imgCaptcha)
    captcha_b64 = await _get_captcha_base64(page)

    # 5) Resolver captcha con OpenAI (reutilizamos tu función de SUNARP)
    captcha_text = await solve_captcha_with_openai(captcha_b64)

    # 6) Input de captcha
    captcha_input = await _get_captcha_input(page)
    if not captcha_input:
        await context.close()
        raise HTTPException(
            status_code=500,
            detail="CITV: no se encontró el input de captcha (texCaptcha)",
        )

    await captcha_input.fill(captcha_text)

    # 7) Botón Buscar
    btn = await _get_buscar_button(page)
    if not btn:
        await context.close()
        raise HTTPException(
            status_code=500,
            detail="CITV: no se encontró el botón 'Buscar'",
        )

    await btn.click()

    # 8) Esperar resultados
    await page.wait_for_timeout(4000)

    try:
        body_text = await page.inner_text("body")
    except Exception:
        await context.close()
        raise HTTPException(
            status_code=500,
            detail="CITV: no se pudo leer el resultado",
        )

    await context.close()

    texto_lower = body_text.lower()

    # Heurística para saber si el captcha fue incorrecto
    captcha_valido = True
    for msg in [
        "captcha incorrecto",
        "código captcha incorrecto",
        "codigo captcha incorrecto",
        "ingresar correctamente el captcha",
    ]:
        if msg in texto_lower:
            captcha_valido = False
            break

    # Heurística para saber si hay certificado vigente
    vigente = "vigente" in texto_lower

    if not captcha_valido:
        return {
            "ok": False,
            "placa": placa,
            "captcha_detectado": captcha_text,
            "captcha_valido": False,
            "vigente": None,
            "mensaje": "El captcha no fue aceptado por la página CITV",
        }

    return {
        "ok": True,
        "placa": placa,
        "captcha_detectado": captcha_text,
        "captcha_valido": True,
        "vigente": vigente,
        "resultado_crudo": body_text,
    }
