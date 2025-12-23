from typing import Any, Dict, List

from fastapi import HTTPException

URL_BUSCAR_DNI = "https://buscardniperu.com/wp-admin/admin-ajax.php"
REFERER_URL = "https://buscardniperu.com/buscar-dni-por-nombres/"


def _clean(value: str) -> str:
    return (value or "").strip().upper()


def _ap_mat_variants(ap_mat: str) -> list[str]:
    """
    Genera algunas variantes simples del apellido materno para mejorar el match
    (útil cuando hay diferencias de escritura como HUILICA vs HUILLCA).
    """
    base = _clean(ap_mat)
    candidatos = []
    seen = set()

    def add(val: str):
        val = _clean(val)
        if val and val not in seen:
            seen.add(val)
            candidatos.append(val)

    add(base)

    # Cambios comunes de letras dobles
    add(base.replace("LL", "L"))
    add(base.replace("L", "LL"))

    # Ajustes para combinaciones I/L frecuentes en apellidos que varían
    if "ILI" in base:
        add(base.replace("ILI", "ILL"))
    if "ILL" in base:
        add(base.replace("ILL", "ILI"))
    add(base.replace("IL", "ILL"))
    add(base.replace("ILL", "IL"))
    add(base.replace("I", "L"))
    add(base.replace("L", "I"))

    # Variantes por inserción/eliminación de caracteres (distancia 1)
    for i, ch in enumerate(base):
        add(base[:i] + ch + base[i:])  # duplica
        add(base[:i] + base[i + 1 :])  # elimina
        if ch == "I":
            add(base[:i] + "L" + base[i + 1 :])
            add(base[:i] + "LL" + base[i + 1 :])
        if ch == "L":
            add(base[:i] + "I" + base[i + 1 :])

    return candidatos[:10]  # límite para evitar demasiados intentos


def _parse_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(entry, dict):
        return {}
    return {
        "dni": (entry.get("dni") or "").strip(),
        "ap_paterno": _clean(entry.get("ap_pat") or entry.get("ap_paterno") or ""),
        "ap_materno": _clean(entry.get("ap_mat") or entry.get("ap_materno") or ""),
        "nombres": _clean(entry.get("nombres") or ""),
        "fecha_nacimiento": (entry.get("fecha_nac") or "").strip(),
        "fecha_inscripcion": (entry.get("fch_inscripcion") or "").strip(),
        "fecha_emision": (entry.get("fch_emision") or "").strip(),
        "fecha_caducidad": (entry.get("fch_caducidad") or "").strip(),
        "ubigeo_nacimiento": (entry.get("ubigeo_nac") or "").strip(),
        "ubigeo_domicilio": (entry.get("ubigeo_dir") or "").strip(),
        "direccion": (entry.get("direccion") or "").strip(),
        "sexo": (entry.get("sexo") or "").strip(),
        "estado_civil": (entry.get("est_civil") or "").strip(),
        "digito_ruc": (entry.get("dig_ruc") or "").strip(),
        "madre": _clean(entry.get("madre") or ""),
        "padre": _clean(entry.get("padre") or ""),
    }


async def _post_busqueda(context, ap_pat: str, ap_mat: str, noms: str, pagina: int):
    response = await context.request.post(
        URL_BUSCAR_DNI,
        form={
            "ap_pat": ap_pat,
            "ap_mat": ap_mat,
            "nombres": noms,
            "pagina": pagina,
            "action": "consulta_dni_api",
            "tipo": "nombre",
        },
        headers={
            "Origin": "https://buscardniperu.com",
            "Referer": REFERER_URL,
        },
    )

    if response.status >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"buscardniperu: respuesta HTTP {response.status}",
        )

    try:
        payload = await response.json()
    except Exception:
        raw_text = await response.text()
        raise HTTPException(
            status_code=500,
            detail=f"buscardniperu: respuesta no es JSON ({raw_text[:200]})",
        )

    success = bool(payload.get("success"))
    data = payload.get("data")
    if not success:
        error_msg = data if isinstance(data, str) else "consulta sin éxito"
        raise HTTPException(status_code=500, detail=f"buscardniperu: {error_msg}")

    filas: List[Dict[str, Any]] = data if isinstance(data, list) else []
    resultados = []
    for fila in filas:
        parsed = _parse_entry(fila)
        if any(parsed.values()):
            resultados.append(parsed)
    return resultados


async def consulta_dni_por_nombres(
    ap_paterno: str, ap_materno: str, nombres: str, browser, pagina: int = 1
) -> Dict[str, Any]:
    """
    Consulta buscardniperu.com por apellidos + nombres para obtener DNI.
    """
    ap_pat = _clean(ap_paterno)
    ap_mat = _clean(ap_materno)
    noms = _clean(nombres)

    if not ap_pat or not ap_mat or not noms:
        raise HTTPException(status_code=400, detail="Se requieren apellidos y nombres para buscar DNI")

    context = await browser.new_context(locale="es-PE")
    resultados: List[Dict[str, Any]] = []
    ap_mat_usado = ap_mat
    intentos = []
    try:
        for variante in _ap_mat_variants(ap_mat):
            try:
                resultados = await _post_busqueda(context, ap_pat, variante, noms, pagina)
                intentos.append({"ap_materno": variante, "total": len(resultados)})
                if resultados:
                    ap_mat_usado = variante
                    break
            except HTTPException:
                raise
            except Exception:
                continue
        if resultados is None:
            raise HTTPException(status_code=500, detail="buscardniperu: no hubo respuesta")
    finally:
        await context.close()

    return {
        "ok": True,
        "busqueda": {
            "ap_paterno": ap_pat,
            "ap_materno": ap_mat,
            "ap_materno_usado": ap_mat_usado,
            "nombres": noms,
            "pagina": pagina,
        },
        "total": len(resultados),
        "sin_resultados": len(resultados) == 0,
        "resultados": resultados,
        "intentos_ap_materno": intentos,
    }
