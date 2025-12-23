import base64
import io

from fastapi import HTTPException
from PIL import Image

from services.sunarp import solve_captcha_with_openai

URL_REDAM = "https://casillas.pj.gob.pe/redam/#/"


async def _select_tab_documento(page):
    try:
        tab = page.get_by_role("link", name="DOCUMENTO DE IDENTIDAD")
        if await tab.count():
            await tab.first.click()
            await page.wait_for_timeout(500)
    except Exception:
        pass


async def _get_captcha_b64(page) -> str:
    img = page.locator("img[src*='Captcha']")
    if not await img.count():
        raise HTTPException(status_code=500, detail="REDAM: no se encontró imagen de captcha")

    raw_png = await img.first.screenshot(type="png")
    pil = Image.open(io.BytesIO(raw_png)).convert("L")
    buf = io.BytesIO()
    pil.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


async def consulta_redam_dni(dni: str, browser):
    """
    Consulta REDAM (casillas.pj.gob.pe/redam) por DNI.
    """
    context = await browser.new_context(locale="es-PE", ignore_https_errors=True)
    page = await context.new_page()

    await page.goto(URL_REDAM, wait_until="domcontentloaded")
    await page.wait_for_timeout(1000)

    await _select_tab_documento(page)

    # Tipo de documento: seleccionar DNI si el select existe
    try:
        select = page.locator("select[ng-model*='tipoDocumento'], select[name*='tipoDocumento']")
        if await select.count():
            await select.select_option(label="DNI")
    except Exception:
        pass

    # Input de documento
    inp_dni = page.locator("#numerodocumento, input[ng-model*='numerodocumento']")
    if not await inp_dni.count():
        await context.close()
        raise HTTPException(status_code=500, detail="REDAM: no se encontró input de documento")
    await inp_dni.fill(dni.strip())

    # Captcha
    captcha_b64 = await _get_captcha_b64(page)
    captcha_text = await solve_captcha_with_openai(captcha_b64)

    captcha_input = page.locator("#captcha, input[ng-model*='captcha']")
    if not await captcha_input.count():
        await context.close()
        raise HTTPException(status_code=500, detail="REDAM: no se encontró input de captcha")
    await captcha_input.fill(captcha_text)

    # Botón Consultar
    btn = page.get_by_role("button", name="CONSULTAR")
    if not await btn.count():
        btn = page.locator("button:has-text('CONSULTAR')")
    if not await btn.count():
        await context.close()
        raise HTTPException(status_code=500, detail="REDAM: no se encontró botón Consultar")

    await btn.first.click()
    await page.wait_for_timeout(4000)

    try:
        body_text = await page.inner_text("body")
    except Exception:
        await context.close()
        raise HTTPException(status_code=500, detail="REDAM: no se pudo leer el resultado")

    # Extraer tabla de resultados si existe
    try:
        tabla = await page.evaluate(
            """() => {
                const tbl = document.querySelector("table.ng-table");
                if (!tbl) return null;
                return Array.from(tbl.querySelectorAll("tr")).map(tr =>
                    Array.from(tr.children).map(td => td.innerText.trim())
                );
            }"""
        )
    except Exception:
        tabla = None

    registros = []
    if tabla and len(tabla) >= 2:
        headers = tabla[0]
        cols = [(i, h) for i, h in enumerate(headers) if h and h != "\xa0"]
        for row in tabla[1:]:
            entry = {}
            for idx, h in cols:
                if idx < len(row):
                    entry[h] = row[idx]
            if entry:
                registros.append(entry)
    else:
        # si solo hay una celda con el mensaje de no registros
        if tabla and len(tabla) == 1 and tabla[0]:
            registros = [{"mensaje": " ".join(tabla[0]).strip()}]

    await context.close()

    texto_lower = body_text.lower()
    captcha_valido = True
    for msg in [
        "captcha incorrecto",
        "código no coincide",
        "código ingresado no es correcto",
    ]:
        if msg in texto_lower:
            captcha_valido = False
            break

    sin_resultados = "no presentan registros" in texto_lower or (
        registros and any("no presentan registros" in (r.get("mensaje", "").lower()) for r in registros)
    )

    return {
        "ok": True,
        "dni": dni,
        "captcha_detectado": captcha_text,
        "captcha_valido": captcha_valido,
        "sin_resultados": sin_resultados,
        "registros": registros,
        "resultado_crudo": body_text,
    }
