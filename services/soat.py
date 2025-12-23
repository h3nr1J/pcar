import asyncio

from fastapi import HTTPException


URL_SOAT = "https://servicios.sbs.gob.pe/reportesoat/"


async def _get_plate_input(page):
    """
    Input de placa:

    <input name="ctl00$MainBodyContent$txtPlaca"
           id="ctl00_MainBodyContent_txtPlaca"
           placeholder="Placa" ...>
    """
    loc = page.locator("#ctl00_MainBodyContent_txtPlaca")
    if await loc.count():
        return loc.first

    # Fallbacks por si cambian algo
    loc = page.get_by_placeholder("Placa")
    if await loc.count():
        return loc.first

    loc = page.locator("input[name='ctl00$MainBodyContent$txtPlaca']")
    if await loc.count():
        return loc.first

    return None


async def _get_consultar_button(page):
    """
    Botón 'Consultar'.
    Usamos primero por rol/nombre y luego por posibles ids.
    """
    # 1) Por accesibilidad (normalmente funciona)
    btn = page.get_by_role("button", name="Consultar")
    if await btn.count():
        return btn.first

    # 2) Por value en un input submit
    loc = page.locator("input[type='submit'][value='Consultar']")
    if await loc.count():
        return loc.first

    # 3) Por id típico ASP.NET
    loc = page.locator("#ctl00_MainBodyContent_btnConsultar")
    if await loc.count():
        return loc.first

    return None


async def consulta_soat(placa: str, browser):
    """
    Hace la consulta de SOAT en la SBS.

    - Va a la página
    - Rellena la placa
    - Se asegura que 'SOAT' esté seleccionado (por defecto lo está)
    - Hace clic en 'Consultar'
    - Lee el texto de la página de resultado
    """
    context = await browser.new_context(locale="es-PE")
    page = await context.new_page()

    # 1) Ir a la página
    await page.goto(URL_SOAT, wait_until="networkidle")
    await page.wait_for_timeout(1000)

    # 2) Escribir la placa (acepta minúsculas)
    placa = placa.strip()
    input_placa = await _get_plate_input(page)
    if not input_placa:
        await context.close()
        raise HTTPException(
            status_code=500,
            detail="SOAT: no se encontró el input de placa en la página",
        )

    await input_placa.fill(placa)

    # (opcional) asegurarnos que el radio "SOAT" esté marcado
    # normalmente ya viene seleccionado
    try:
        radio_soat = page.get_by_label("SOAT")
        if await radio_soat.count():
            await radio_soat.first.check()
    except Exception:
        pass  # si falla, no pasa nada, la página ya lo suele tener marcado

    # 3) Botón Consultar
    btn = await _get_consultar_button(page)
    if not btn:
        await context.close()
        raise HTTPException(
            status_code=500,
            detail="SOAT: no se encontró el botón 'Consultar'",
        )

    await btn.click()

    # 4) Esperar resultado
    # Le damos unos segundos para que el servidor responda y se renderice la tabla
    await page.wait_for_timeout(4000)

    try:
        body_text = await page.inner_text("body")
    except Exception:
        await context.close()
        raise HTTPException(
            status_code=500,
            detail="SOAT: no se pudo leer el resultado de la página",
        )

    await context.close()

    texto_lower = body_text.lower()

    # Mensaje cuando NO hay información:
    # "La placa consultada no tiene información reportada sobre SOAT"
    sin_info = "no tiene información reportada sobre soat" in texto_lower

    return {
        "ok": True,
        "placa": placa,
        "tiene_informacion": not sin_info,
        "sin_informacion": sin_info,
        "resultado_crudo": body_text,
    }
