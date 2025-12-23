from contextlib import asynccontextmanager
from typing import Iterable, Optional

from fastapi import HTTPException


@asynccontextmanager
async def use_page(
    browser,
    *,
    locale: str = "es-PE",
    ignore_https_errors: bool = False,
    **context_kwargs,
):
    """
    Crea un nuevo contexto y página de Playwright y garantiza el cierre.
    """
    context = await browser.new_context(
        locale=locale,
        ignore_https_errors=ignore_https_errors,
        **context_kwargs,
    )
    page = await context.new_page()
    try:
        yield page
    finally:
        await context.close()


async def goto_or_fail(
    page,
    url: str,
    *,
    wait_until: str = "domcontentloaded",
    timeout: int = 30000,
    error_detail: Optional[str] = None,
):
    """
    Va a la URL indicada y levanta un HTTPException 502 si falla.
    """
    detail = error_detail or f"No se pudo cargar la URL {url}"
    try:
        await page.goto(url, wait_until=wait_until, timeout=timeout)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"{detail} ({e})")


async def first_locator(scope, selectors: Iterable[str]):
    """
    Devuelve el primer locator que exista entre varios selectores.
    """
    for sel in selectors:
        loc = scope.locator(sel)
        if await loc.count():
            return loc.first
    return None


async def expect_locator(scope, selectors: Iterable[str], *, not_found_detail: str):
    """
    Igual que first_locator pero lanza HTTP 500 si no encuentra nada.
    """
    loc = await first_locator(scope, selectors)
    if not loc:
        raise HTTPException(status_code=500, detail=not_found_detail)
    return loc


async def inner_text_or_empty(scope, selector: str = "body") -> str:
    """
    Lee inner_text del selector indicado y devuelve '' en caso de error.
    """
    try:
        return await scope.inner_text(selector)
    except Exception:
        return ""
