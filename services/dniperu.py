from typing import Dict

from fastapi import HTTPException

URL_DNI_PERU = "https://dniperu.com/buscar-dni-nombres-apellidos/"


def _parse_textarea(texto: str) -> Dict[str, str]:
    datos: Dict[str, str] = {}
    for line in texto.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip().lower()
        val = val.strip()
        if not val:
            continue
        if "numero" in key and "dni" in key:
            datos["dni"] = val
        elif "nombres" in key:
            datos["nombres"] = val
        elif "paterno" in key:
            datos["apellido_paterno"] = val
        elif "materno" in key:
            datos["apellido_materno"] = val
        elif "verificacion" in key:
            datos["codigo_verificacion"] = val
    return datos


async def consulta_dni_peru(dni: str, browser):
    dni = dni.strip()
    if not dni:
        raise HTTPException(status_code=400, detail="DNI vacío")

    context = await browser.new_context(locale="es-PE")
    page = await context.new_page()
    await page.goto(URL_DNI_PERU, wait_until="domcontentloaded")
    await page.wait_for_timeout(1000)

    inp = page.locator("#dni4")
    if not await inp.count():
        await context.close()
        raise HTTPException(status_code=500, detail="No se encontró el input de DNI en dniperu.com")
    await inp.fill(dni)

    btn = page.locator("#buscar-dni-button")
    if not await btn.count():
        await context.close()
        raise HTTPException(status_code=500, detail="No se encontró el botón Buscar en dniperu.com")
    await btn.click()
    await page.wait_for_timeout(3000)

    # Esperar el textarea u otra fuente de texto y leer su contenido
    raw_text = ""
    selectors = [
        "#resultado_dni",
        "#resultado-nombres",
        'textarea[name="resultado_dni"]',
        'textarea[id*="resultado"]',
        "textarea",
        "pre",
        "code",
    ]

    textarea = None
    for sel in selectors:
        try:
            await page.wait_for_selector(sel, timeout=5000)
            loc = page.locator(sel)
            if await loc.count():
                textarea = loc.first
                break
        except Exception:
            continue

    if textarea:
        try:
            raw_text = (await textarea.input_value()).strip()
        except Exception:
            try:
                raw_text = (await textarea.text_content() or "").strip()
            except Exception:
                raw_text = ""

    # Fallback: tomar texto de un posible pre/code o del body
    if not raw_text:
        try:
            raw_text = await page.evaluate(
                """() => {
                    const el = document.querySelector('#resultado_dni, textarea, pre, code');
                    return el ? (el.value || el.innerText || el.textContent || '').trim() : '';
                }"""
            )
        except Exception:
            raw_text = ""

    if not raw_text:
        await context.close()
        raise HTTPException(status_code=500, detail="No se encontró el textarea de resultado en dniperu.com")

    parsed = _parse_textarea(raw_text)

    await context.close()

    return {
        "ok": True,
        "dni_consultado": dni,
        "resultado_crudo": raw_text,
        "datos": parsed,
    }
