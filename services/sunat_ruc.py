import re
from html import unescape

import httpx
from fastapi import HTTPException

BASE_URL_SUNAT = "https://e-consultaruc.sunat.gob.pe/cl-ti-itmrconsruc/"
URL_SUNAT_RUC = f"{BASE_URL_SUNAT}FrameCriterioBusquedaWeb.jsp"
URL_POST_BUSQUEDA = f"{BASE_URL_SUNAT}jcrS00Alias"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-PE,es;q=0.9,en;q=0.8",
}

_RUC_RE = re.compile(r"\b(\d{11})\b")
_RUC_LINE_RE = re.compile(r"RUC\s*[:\-]?\s*(\d{11})", re.IGNORECASE)
_FECHA_HTML_RE = re.compile(r"Fecha consulta\s*:\s*([^<]+)</small>", re.IGNORECASE)
_ITEM_HTML_RE = re.compile(
    r"<a\b[^>]*data-ruc=['\"](\d{11})['\"][^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_ESTADO_HTML_RE = re.compile(
    r"Estado\s*:\s*(?:<strong>)?(?:<span[^>]*>)?(.*?)(?:</span>)?(?:</strong>)?\s*</p>",
    re.IGNORECASE | re.DOTALL,
)
_UBICACION_HTML_RE = re.compile(
    r"Ubicaci(?:&oacute;|ó|o)n\s*:\s*(.*?)</p>",
    re.IGNORECASE | re.DOTALL,
)
_H4_RE = re.compile(r"<h4\b[^>]*>(.*?)</h4>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _html_to_text(html_text: str) -> str:
    text = _TAG_RE.sub(" ", html_text or "")
    text = unescape(text).replace("\xa0", " ")
    return _clean(text)


def _extract_text(regex: re.Pattern, html_text: str) -> str:
    m = regex.search(html_text or "")
    if not m:
        return ""
    return _html_to_text(m.group(1))


def _parse_item_html(ruc: str, html_item: str) -> dict:
    razon_social = ""
    for raw_h4 in _H4_RE.findall(html_item or ""):
        t = _html_to_text(raw_h4)
        if not t:
            continue
        if _RUC_LINE_RE.search(t) or _RUC_RE.fullmatch(t):
            continue
        razon_social = t
        break

    return {
        "ruc": ruc,
        "razon_social": razon_social,
        "ubicacion": _extract_text(_UBICACION_HTML_RE, html_item),
        "estado": _extract_text(_ESTADO_HTML_RE, html_item),
    }


def _parse_html_results(html_text: str) -> list[dict]:
    resultados = []
    seen = set()
    for ruc, block in _ITEM_HTML_RE.findall(html_text or ""):
        if ruc in seen:
            continue
        seen.add(ruc)
        item = _parse_item_html(ruc, block)
        if item.get("ruc"):
            resultados.append(item)
    return resultados


def _parse_text_fallback(text: str) -> list[dict]:
    lines = [_clean(x) for x in (text or "").splitlines() if _clean(x)]
    if not lines:
        return []

    resultados = []
    seen = set()
    for idx, line in enumerate(lines):
        ruc_match = _RUC_LINE_RE.search(line) or _RUC_RE.search(line)
        if not ruc_match:
            continue
        ruc = ruc_match.group(1)
        if ruc in seen:
            continue
        seen.add(ruc)
        razon = ""
        for k in range(1, 4):
            if idx + k >= len(lines):
                break
            candidate = lines[idx + k]
            upper = candidate.upper()
            if upper.startswith(("UBICACION", "UBICACIÓN", "ESTADO", "FECHA CONSULTA", "VOLVER")):
                continue
            if _RUC_RE.search(candidate):
                continue
            razon = candidate
            break
        resultados.append({"ruc": ruc, "razon_social": razon, "ubicacion": "", "estado": ""})
    return resultados


def _contains_no_results(text: str) -> bool:
    t = (text or "").upper()
    hints = (
        "NO REGISTRA UN NUMERO DE RUC",
        "NO REGISTRA UN NÚMERO DE RUC",
        "NO SE ENCONTRO INFORMACION",
        "NO SE ENCONTRÓ INFORMACIÓN",
        "SIN RESULTADOS",
    )
    return any(h in t for h in hints)


async def consulta_sunat_ruc_por_nombre(nombre: str, _browser=None):
    termino = _clean(nombre)
    if len(termino) < 3:
        raise HTTPException(
            status_code=400,
            detail="Debe ingresar al menos 3 caracteres en nombre o razon social",
        )

    payload = {
        "accion": "consPorRazonSoc",
        "razSoc": termino,
        "search3": termino,
        "nroRuc": "",
        "search1": "",
        "nrodoc": "",
        "search2": "",
        "tipdoc": "1",
        "token": "",
        "contexto": "ti-it",
        "modo": "1",
        "codigo": "",
    }
    headers = {**DEFAULT_HEADERS, "Referer": URL_SUNAT_RUC}

    try:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers=headers,
            http2=False,
        ) as client:
            warmup = await client.get(URL_SUNAT_RUC)
            if warmup.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail=f"SUNAT RUC: warmup devolvio HTTP {warmup.status_code}",
                )
            resp = await client.post(URL_POST_BUSQUEDA, data=payload)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"SUNAT RUC: error de conexion al consultar ({e})",
        )

    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"SUNAT RUC: busqueda devolvio HTTP {resp.status_code}",
        )

    html_result = resp.text or ""
    text_result = _html_to_text(html_result)

    resultados = _parse_html_results(html_result)
    if not resultados:
        fallback = _parse_text_fallback(text_result)
        seen = set()
        for row in fallback:
            ruc = row.get("ruc")
            if not ruc or ruc in seen:
                continue
            seen.add(ruc)
            resultados.append(row)

    fecha = _extract_text(_FECHA_HTML_RE, html_result)
    no_results = len(resultados) == 0 and _contains_no_results(text_result)

    return {
        "ok": True,
        "modo_busqueda": "nombre_razon_social",
        "nombre_buscado": termino,
        "url_resultado": str(resp.url),
        "total": len(resultados),
        "sin_resultados": len(resultados) == 0,
        "sin_resultados_confirmado": no_results,
        "fecha_consulta": fecha,
        "resultados": resultados,
        "resultado_crudo": text_result,
    }
