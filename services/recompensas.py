from typing import Any, Dict, List

from fastapi import HTTPException

from services.playwright_utils import (
    expect_locator,
    first_locator,
    goto_or_fail,
    inner_text_or_empty,
    use_page,
)
from services.sunarp import consulta_sunarp

URL_RECOMPENSAS = "https://recompensas.pe/requisitoriados"


async def _find_nombre_input(page):
    return await first_locator(
        page,
        [
            'input[name="nombreCompleto"]',
            'input[placeholder*="Nombre" i]',
            'input[placeholder*="apellido" i]',
        ],
    )


async def _click_buscar(page):
    # Intento por rol accesible primero
    btn = page.get_by_role("button", name="BUSCAR")
    if await btn.count():
        btn = btn.first
    else:
        btn = await expect_locator(
            page,
            [
                "button:has-text('BUSCAR')",
                "button:has-text('Buscar')",
                'button[type="submit"]',
            ],
            not_found_detail="Recompensas: botón Buscar no encontrado",
        )

    # Espera a que no esté disabled y sea visible
    try:
        await btn.wait_for(state="visible", timeout=5000)
    except Exception:
        pass
    for _ in range(20):
        disabled_attr = await btn.get_attribute("disabled")
        if not disabled_attr:
            break
        await page.wait_for_timeout(300)
    await btn.click()


async def _parse_cards(page) -> List[Dict[str, Any]]:
    try:
        cards = await page.evaluate(
            """() => Array.from(document.querySelectorAll('div.card.h-100')).map(card => {
                const name = card.querySelector('.card-title')?.innerText?.trim() || '';
                const recompensa = card.querySelector('.card-text')?.innerText?.trim() || '';
                const img = card.querySelector('img')?.getAttribute('src') || '';
                return { nombre: name, recompensa, imagen: img };
            });"""
        )
    except Exception:
        cards = []

    clean = []
    for c in cards or []:
        name = (c.get("nombre") or "").strip()
        recompensa = (c.get("recompensa") or "").strip()
        imagen = (c.get("imagen") or "").strip()
        if name or recompensa or imagen:
            clean.append({"nombre": name, "recompensa": recompensa, "imagen": imagen})
    return clean


async def consulta_recompensas_por_nombre(nombre: str, browser):
    """
    Busca en recompensas.pe/requisitoriados usando el nombre completo.
    """
    nombre = nombre.strip()
    if not nombre:
        raise HTTPException(status_code=400, detail="Nombre vacío para consulta de recompensas")

    async with use_page(browser, locale="es-PE", ignore_https_errors=True) as page:
        await goto_or_fail(
            page,
            URL_RECOMPENSAS,
            error_detail="Recompensas: no se pudo cargar la web",
        )
        await page.wait_for_timeout(1000)

        nombre_input = await _find_nombre_input(page)
        if not nombre_input:
            raise HTTPException(
                status_code=500, detail="Recompensas: input de nombre no encontrado"
            )

        await nombre_input.fill(nombre)
        await _click_buscar(page)
        await page.wait_for_timeout(3000)

        resultados = await _parse_cards(page)
        body_text = await inner_text_or_empty(page, "body")

        return {
            "ok": True,
            "nombre_busqueda": nombre,
            "total": len(resultados),
            "sin_resultados": len(resultados) == 0,
            "resultados": resultados,
            "resultado_crudo": body_text,
        }


def _build_nombre_completo(propietario: Dict[str, Any]) -> str:
    parts = [
        propietario.get("ap_paterno", ""),
        propietario.get("ap_materno", ""),
        propietario.get("nombres", ""),
    ]
    nombre = " ".join(p for p in parts if p).strip()
    if not nombre:
        nombre = (propietario.get("texto") or "").strip()
    return nombre


def obtener_nombre_desde_propietarios(propietarios: List[Dict[str, Any]]) -> str:
    """
    Devuelve el primer nombre completo utilizable a partir de la lista de
    propietarios_detalle de SUNARP.
    """
    for prop in propietarios or []:
        nombre = _build_nombre_completo(prop)
        if nombre:
            return nombre
    return ""


async def consulta_recompensas_desde_propietarios(propietarios: List[Dict[str, Any]], browser):
    """
    Usa una lista de propietarios (p.ej. propietarios_detalle de SUNARP)
    para buscar en recompensas.pe sin volver a consultar SUNARP.
    """
    nombre_objetivo = obtener_nombre_desde_propietarios(propietarios)
    if not nombre_objetivo:
        return {
            "ok": False,
            "mensaje": "No se pudo obtener un nombre de propietario desde SUNARP",
        }
    return await consulta_recompensas_por_nombre(nombre_objetivo, browser)


async def consulta_recompensas_desde_sunarp(placa: str, browser):
    """
    Consulta SUNARP y usa el primer propietario detectado para buscar en recompensas.pe.
    """
    sunarp_res = await consulta_sunarp(placa, browser)

    propietarios = sunarp_res.get("propietarios_detalle") or []
    nombre_objetivo = ""
    for prop in propietarios:
        nombre_objetivo = _build_nombre_completo(prop)
        if nombre_objetivo:
            break

    if not nombre_objetivo:
        return {
            "ok": False,
            "mensaje": "No se pudo obtener un nombre de propietario desde SUNARP",
            "sunarp": sunarp_res,
        }

    recompensas_res = await consulta_recompensas_por_nombre(nombre_objetivo, browser)

    return {
        "ok": True,
        "propietario_usado": nombre_objetivo,
        "sunarp": sunarp_res,
        "recompensas": recompensas_res,
    }
