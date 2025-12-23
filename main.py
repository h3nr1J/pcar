# main.py
import os
import asyncio
import base64
from time import perf_counter
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from services.soat import consulta_soat
from services.revision import consulta_revision  
from services.sat import consulta_sat
from services.satcallao import consulta_satcallao
from services.licencia import (
    consulta_licencia_por_nombre,
    consulta_licencia_por_dni,
    iniciar_sesion_licencia_dni,
    iniciar_sesion_licencia_nombre,
    enviar_captcha_sesion_licencia,
    get_captcha_b64_sesion_licencia,
)
from services.sunarp import consulta_sunarp
from services.sutran import consulta_sutran
from services.redam import consulta_redam_dni
from services.recompensas import (
    consulta_recompensas_por_nombre,
    consulta_recompensas_desde_sunarp,
    consulta_recompensas_desde_propietarios,
)
from services.redam import consulta_redam_dni
from services.buscardniperu import consulta_dni_por_nombres
from services.dniperu import consulta_dni_peru

load_dotenv()

SERVICE_TIMEOUT_MS = int(os.getenv("SERVICE_TIMEOUT_MS", "20000"))
RECOMPENSAS_TIMEOUT_MS = int(os.getenv("RECOMPENSAS_TIMEOUT_MS", "25000"))
LICENCIA_TIMEOUT_MS = int(os.getenv("LICENCIA_TIMEOUT_MS", "40000"))


class ConsultaRequest(BaseModel):
    placa: str


class ConsultaVehicularFullRequest(BaseModel):
    placa: str
    servicios: list[str] | None = None
    dni: str | None = None


class LicenciaNombreRequest(BaseModel):
    ap_paterno: str = Field(..., alias="apellido_paterno")
    ap_materno: str = Field(..., alias="apellido_materno")
    nombre: str = Field(..., alias="nombres")

    model_config = {
        # permite usar tanto los nombres originales como los alias del JSON
        "populate_by_name": True,
        "extra": "ignore",
    }


class LicenciaDniRequest(BaseModel):
    dni: str


class LicenciaCaptchaSubmitRequest(BaseModel):
    session_id: str
    captcha_text: str


class RedamDniRequest(BaseModel):
    dni: str


class RecompensasNombreRequest(BaseModel):
    nombre: str


class DniNombreRequest(BaseModel):
    ap_paterno: str = Field(..., alias="apellido_paterno")
    ap_materno: str = Field(..., alias="apellido_materno")
    nombres: str

    model_config = {
        "populate_by_name": True,
        "extra": "ignore",
    }


class DniPeruRequest(BaseModel):
    dni: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Inicia Playwright y el navegador una sola vez para todo el proceso.
    """
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    headless = os.getenv("HEADLESS", "1").lower() not in {"0", "false"}
    # --no-sandbox evita errores en entornos sin sandbox de Chrome (contenedores, CI)
    browser = await pw.chromium.launch(headless=headless, args=["--no-sandbox"])

    app.state.pw = pw
    app.state.browser = browser
    try:
        yield
    finally:
        await browser.close()
        await pw.stop()


app = FastAPI(lifespan=lifespan)

# CORS para permitir llamadas desde Expo / web
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ajusta a los orígenes de tu app si quieres restringir
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SERVICIOS_VEHICULARES = {
    "sunarp": consulta_sunarp,
    "soat": consulta_soat,
    "revision": consulta_revision,
    "sutran": consulta_sutran,
    "sat": consulta_sat,
    "sat_callao": consulta_satcallao,
}

DEFAULT_SERVICIOS_VEHICULO = [
    "sunarp",
    "soat",
    "revision",
    "sutran",
    "sat",
    "sat_callao",
    "dni_nombre",
    "licencia",
    "redam",
    "recompensas",
]

SERVICIOS_TODOS = set(DEFAULT_SERVICIOS_VEHICULO) | {"dni_peru"}

SERVICIO_ALIASES = {
    "dni": "dni_peru",
    "dniperu": "dni_peru",
    "dni_nombre": "dni_nombre",
    "dni_nombres": "dni_nombre",
    "dni_propietario": "dni_nombre",
    "dni_por_nombre": "dni_nombre",
    "lic": "licencia",
    "mtc_licencia": "licencia",
}


def _normalizar_servicios(lista: list[str] | None) -> list[str]:
    """
    Devuelve la lista de servicios a ejecutar, normalizada y sin duplicados.
    """
    if not lista:
        return DEFAULT_SERVICIOS_VEHICULO.copy()
    normalizados = []
    invalidos = []
    for item in lista:
        slug = (item or "").strip().lower()
        slug = SERVICIO_ALIASES.get(slug, slug)
        if not slug:
            continue
        if slug not in SERVICIOS_TODOS:
            invalidos.append(item)
            continue
        if slug not in normalizados:
            normalizados.append(slug)
    if invalidos:
        raise HTTPException(
            status_code=400,
            detail=f"Servicios no soportados: {', '.join(invalidos)}",
        )
    return normalizados


async def _wrap_servicio(nombre: str, fn, placa: str, browser):
    """
    Ejecuta un servicio y captura errores sin lanzar excepciones al cliente.
    """
    started = perf_counter()
    try:
        data = await asyncio.wait_for(fn(placa, browser), timeout=SERVICE_TIMEOUT_MS / 1000)
        inner_ok = data.get("ok", True) if isinstance(data, dict) else True
        return {
            "ok": bool(inner_ok),
            "data": data,
            "error": None,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "error": f"Timeout después de {SERVICE_TIMEOUT_MS} ms",
            "status": 504,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }
    except HTTPException as e:
        return {
            "ok": False,
            "error": e.detail,
            "status": e.status_code,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "status": 500,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }


async def _wrap_recompensas(placa: str, browser, sunarp_res: dict | None):
    """
    Ejecuta recompensas intentando reutilizar propietarios de SUNARP si ya se consultó.
    """
    started = perf_counter()
    try:
        if sunarp_res and sunarp_res.get("ok"):
            sunarp_data = sunarp_res.get("data") or {}
            propietarios = sunarp_data.get("propietarios_detalle") or []
            data = await asyncio.wait_for(
                consulta_recompensas_desde_propietarios(propietarios, browser),
                timeout=RECOMPENSAS_TIMEOUT_MS / 1000,
            )
        else:
            data = await asyncio.wait_for(
                consulta_recompensas_desde_sunarp(placa, browser),
                timeout=RECOMPENSAS_TIMEOUT_MS / 1000,
            )

        inner_ok = data.get("ok", True) if isinstance(data, dict) else True
        return {
            "ok": bool(inner_ok),
            "data": data,
            "error": None,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "error": f"Timeout después de {RECOMPENSAS_TIMEOUT_MS} ms",
            "status": 504,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }
    except HTTPException as e:
        return {
            "ok": False,
            "error": e.detail,
            "status": e.status_code,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "status": 500,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }


def _extraer_propietario_sunarp(sunarp_res: dict | None):
    """
    Toma el bloque de resultado de sunarp en el agregador
    y devuelve el primer propietario con nombres completos.
    """
    if not sunarp_res or not sunarp_res.get("ok"):
        return None
    data = sunarp_res.get("data") or {}
    propietarios = data.get("propietarios_detalle") or []
    for p in propietarios:
        ap_pat = (p.get("ap_paterno") or "").strip()
        ap_mat = (p.get("ap_materno") or "").strip()
        nombres = (p.get("nombres") or "").strip()
        if ap_pat and ap_mat and nombres:
            return {"ap_paterno": ap_pat, "ap_materno": ap_mat, "nombres": nombres}
    return None


async def _wrap_licencia_desde_sunarp(sunarp_res: dict | None, browser):
    """
    Ejecuta la consulta de licencia usando el primer propietario de SUNARP.
    """
    started = perf_counter()
    propietario = _extraer_propietario_sunarp(sunarp_res)
    if not propietario:
        return {
            "ok": False,
            "error": "No hay propietario válido en SUNARP para buscar licencia",
            "status": 400,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }
    try:
        data = await asyncio.wait_for(
            consulta_licencia_por_nombre(
                propietario["ap_paterno"],
                propietario["ap_materno"],
                propietario["nombres"],
                browser,
            ),
            timeout=LICENCIA_TIMEOUT_MS / 1000,
        )
        inner_ok = data.get("ok", True) if isinstance(data, dict) else True
        return {
            "ok": bool(inner_ok),
            "data": data,
            "error": None,
            "duracion_ms": int((perf_counter() - started) * 1000),
            "propietario_usado": propietario,
        }
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "error": f"Timeout después de {LICENCIA_TIMEOUT_MS} ms",
            "status": 504,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }
    except HTTPException as e:
        return {
            "ok": False,
            "error": e.detail,
            "status": e.status_code,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "status": 500,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }


async def _wrap_dni_nombre_desde_sunarp(sunarp_res: dict | None, browser):
    """
    Obtiene un DNI consultando buscardniperu.com con el primer propietario de SUNARP.
    """
    started = perf_counter()
    propietario = _extraer_propietario_sunarp(sunarp_res)
    if not propietario:
        return {
            "ok": False,
            "error": "No hay propietario válido en SUNARP para buscar DNI",
            "status": 400,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }
    try:
        data = await asyncio.wait_for(
            consulta_dni_por_nombres(
                propietario["ap_paterno"],
                propietario["ap_materno"],
                propietario["nombres"],
                browser,
            ),
            timeout=SERVICE_TIMEOUT_MS / 1000,
        )
        inner_ok = data.get("ok", True) if isinstance(data, dict) else True
        return {
            "ok": bool(inner_ok),
            "data": data,
            "error": None,
            "duracion_ms": int((perf_counter() - started) * 1000),
            "propietario_usado": propietario,
        }
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "error": f"Timeout después de {SERVICE_TIMEOUT_MS} ms",
            "status": 504,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }
    except HTTPException as e:
        return {
            "ok": False,
            "error": e.detail,
            "status": e.status_code,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "status": 500,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }


def _dni_desde_licencia(lic_res: dict | None) -> str | None:
    """
    Extrae el DNI del resumen de licencia si está presente.
    """
    if not lic_res or not lic_res.get("ok"):
        return None
    data = lic_res.get("data") or {}
    resumen = data.get("resumen") or {}
    dni = resumen.get("dni") or resumen.get("documento") or ""
    dni = dni.strip()
    return dni or None


def _dni_desde_dni_peru(dni_res: dict | None) -> str | None:
    """
    Extrae el DNI del servicio dniperu.
    """
    if not dni_res or not dni_res.get("ok"):
        return None
    data = dni_res.get("data") or {}
    datos = data.get("datos") or {}
    dni = datos.get("dni") or ""
    dni = dni.strip()
    return dni or None


def _dni_desde_dni_nombre(dni_res: dict | None) -> str | None:
    """
    Extrae el DNI obtenido por nombres (buscardniperu).
    """
    if not dni_res or not dni_res.get("ok"):
        return None
    data = dni_res.get("data") or {}
    resultados = data.get("resultados") or []
    for fila in resultados:
        dni = (fila.get("dni") or "").strip()
        if dni:
            return dni
    return None


async def _wrap_redam(dni: str | None, browser):
    """
    Ejecuta REDAM por DNI con timeout y manejo de errores.
    """
    started = perf_counter()
    if not dni:
        return {
            "ok": False,
            "error": "dni requerido para el servicio redam",
            "status": 400,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }
    try:
        data = await asyncio.wait_for(
            consulta_redam_dni(dni, browser), timeout=SERVICE_TIMEOUT_MS / 1000
        )
        inner_ok = data.get("ok", True) if isinstance(data, dict) else True
        return {
            "ok": bool(inner_ok),
            "data": data,
            "error": None,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "error": f"Timeout después de {SERVICE_TIMEOUT_MS} ms",
            "status": 504,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }
    except HTTPException as e:
        return {
            "ok": False,
            "error": e.detail,
            "status": e.status_code,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "status": 500,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }


async def _wrap_licencia_por_dni(dni: str, browser):
    """
    Ejecuta licencia por DNI con timeout propio.
    """
    started = perf_counter()
    try:
        data = await asyncio.wait_for(
            consulta_licencia_por_dni(dni, browser), timeout=LICENCIA_TIMEOUT_MS / 1000
        )
        inner_ok = data.get("ok", True) if isinstance(data, dict) else True
        return {
            "ok": bool(inner_ok),
            "data": data,
            "error": None,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "error": f"Timeout después de {LICENCIA_TIMEOUT_MS} ms",
            "status": 504,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }
    except HTTPException as e:
        return {
            "ok": False,
            "error": e.detail,
            "status": e.status_code,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "status": 500,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }


async def _wrap_dni_peru(dni: str | None, browser):
    """
    Ejecuta la consulta de DNI->nombres/apellidos si se proporciona DNI.
    """
    started = perf_counter()
    if not dni:
        return {
            "ok": False,
            "error": "dni requerido para el servicio dni_peru",
            "status": 400,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }
    try:
        data = await asyncio.wait_for(
            consulta_dni_peru(dni, browser), timeout=SERVICE_TIMEOUT_MS / 1000
        )
        inner_ok = data.get("ok", True) if isinstance(data, dict) else True
        return {
            "ok": bool(inner_ok),
            "data": data,
            "error": None,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "error": f"Timeout después de {SERVICE_TIMEOUT_MS} ms",
            "status": 504,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }
    except HTTPException as e:
        return {
            "ok": False,
            "error": e.detail,
            "status": e.status_code,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "status": 500,
            "duracion_ms": int((perf_counter() - started) * 1000),
        }


@app.get("/")
async def root():
    return {
        "ok": True,
        "message": "API de consultas vehiculares (SUNARP, SOAT, CITV, ...)",
    }


@app.post("/consulta-vehicular-full")
async def consulta_vehicular_full(req: ConsultaVehicularFullRequest):
    """
    Endpoint agregador: ejecuta varias consultas vehiculares en paralelo
    y devuelve un bloque por servicio solicitado.
    """
    browser = app.state.browser
    placa = req.placa.strip().upper()
    servicios = _normalizar_servicios(req.servicios)

    resultados = {}
    tareas = {}
    sunarp_task = None
    dni_peru_task = None

    for nombre in servicios:
        if nombre == "sunarp":
            sunarp_task = asyncio.create_task(_wrap_servicio("sunarp", consulta_sunarp, placa, browser))
            continue
        if nombre == "dni_peru":
            dni_peru_task = asyncio.create_task(_wrap_dni_peru(req.dni, browser))
            continue
        if nombre in {"recompensas", "licencia", "redam", "dni_nombre"}:
            continue  # dependen de otros datos
        fn = SERVICIOS_VEHICULARES.get(nombre)
        if not fn:
            continue
        tareas[nombre] = asyncio.create_task(_wrap_servicio(nombre, fn, placa, browser))

    # Esperar tareas independientes
    for nombre, tarea in tareas.items():
        resultados[nombre] = await tarea

    if sunarp_task:
        resultados["sunarp"] = await sunarp_task

    if dni_peru_task:
        resultados["dni_peru"] = await dni_peru_task

    if "dni_nombre" in servicios:
        resultados["dni_nombre"] = await _wrap_dni_nombre_desde_sunarp(
            resultados.get("sunarp"),
            browser,
        )

    # Recompensas: intenta usar los propietarios de SUNARP ya obtenidos
    if "recompensas" in servicios:
        resultados["recompensas"] = await _wrap_recompensas(
            placa,
            browser,
            resultados.get("sunarp"),
        )

    # Licencia: preferir DNI si lo tenemos (request o dniperu), luego SUNARP por nombres
    if "licencia" in servicios:
        dni_para_licencia = (
            req.dni
            or _dni_desde_dni_peru(resultados.get("dni_peru"))
            or _dni_desde_dni_nombre(resultados.get("dni_nombre"))
        )
        if dni_para_licencia:
            resultados["licencia"] = await _wrap_licencia_por_dni(dni_para_licencia, browser)
        else:
            resultados["licencia"] = await _wrap_licencia_desde_sunarp(
                resultados.get("sunarp"),
                browser,
            )

    # REDAM: usa DNI directo, luego licencia, luego dniperu
    if "redam" in servicios:
        dni_redam = (
            req.dni
            or _dni_desde_licencia(resultados.get("licencia"))
            or _dni_desde_dni_peru(resultados.get("dni_peru"))
            or _dni_desde_dni_nombre(resultados.get("dni_nombre"))
        )
        resultados["redam"] = await _wrap_redam(dni_redam, browser)

    return {
        "ok": True,
        "placa": placa,
        "dni": req.dni,
        "servicios": resultados,
        "orden_solicitado": servicios,
    }


# -------- SUNARP --------
@app.post("/consulta-vehicular")
async def consulta_vehicular(req: ConsultaRequest):
    """
    Consulta vehicular en SUNARP.
    """
    browser = app.state.browser
    return await consulta_sunarp(req.placa, browser)

@app.post("/consulta-vehicular-imagen")
async def consulta_vehicular_imagen(req: ConsultaRequest):
    """
    Consulta vehicular en SUNARP y devuelve la imagen como PNG (útil para Postman).
    """
    browser = app.state.browser
    data = await consulta_sunarp(req.placa, browser)
    src = (data or {}).get("imagen_resultado_src")
    if not src:
        raise HTTPException(status_code=500, detail="SUNARP: no devolvió imagen de resultado")

    if src.startswith("data:") and "base64," in src:
        b64 = src.split("base64,", 1)[-1]
        try:
            raw = base64.b64decode(b64)
        except Exception:
            raise HTTPException(status_code=500, detail="SUNARP: no se pudo decodificar la imagen")
        return Response(
            content=raw,
            media_type="image/png",
            headers={"Content-Disposition": "inline; filename=sunarp.png"},
        )

    raise HTTPException(status_code=500, detail="SUNARP: formato de imagen no soportado")


# -------- SOAT --------
@app.post("/consulta-soat")
async def consulta_soat_endpoint(req: ConsultaRequest):
    """
    Consulta de siniestralidad SOAT (SBS).
    """
    browser = app.state.browser
    return await consulta_soat(req.placa, browser)


# -------- INSPECCIÓN TÉCNICA (CITV) --------
@app.post("/consulta-itv")
async def consulta_itv_endpoint(req: ConsultaRequest):
    """
    Consulta de certificados de Inspección Técnica Vehicular (CITV) – MTC.
    """
    browser = app.state.browser
    return await consulta_revision(req.placa, browser)
@app.post("/consulta-sat")
async def endpoint_sat(req: ConsultaRequest):
    browser = app.state.browser
    return await consulta_sat(req.placa.upper(), browser)


# -------- LICENCIA POR NOMBRES --------
@app.post("/consulta-licencia-nombre")
async def consulta_licencia_nombre(req: LicenciaNombreRequest):
    """
    Consulta licencias en slcp.mtc.gob.pe buscando por apellidos y nombre completo.
    """
    browser = app.state.browser
    return await consulta_licencia_por_nombre(
        req.ap_paterno, req.ap_materno, req.nombre, browser
    )


@app.post("/consulta-licencia-nombre-init")
async def consulta_licencia_nombre_init(req: LicenciaNombreRequest):
    """
    Inicia una sesión para consulta de licencia por nombres y devuelve el captcha (para resolverlo manualmente).
    """
    browser = app.state.browser
    return await iniciar_sesion_licencia_nombre(req.ap_paterno, req.ap_materno, req.nombre, browser)


@app.post("/consulta-licencia-dni")
async def consulta_licencia_dni(req: LicenciaDniRequest):
    """
    Consulta licencias en slcp.mtc.gob.pe buscando por N° de documento (DNI).
    """
    browser = app.state.browser
    return await consulta_licencia_por_dni(req.dni, browser)


@app.post("/consulta-licencia-dni-init")
async def consulta_licencia_dni_init(req: LicenciaDniRequest):
    """
    Inicia una sesión para consulta de licencia por DNI y devuelve el captcha (para resolverlo manualmente).
    """
    browser = app.state.browser
    return await iniciar_sesion_licencia_dni(req.dni, browser)


@app.post("/consulta-licencia-submit")
async def consulta_licencia_submit(req: LicenciaCaptchaSubmitRequest):
    """
    Envía el captcha resuelto por el usuario para una sesión iniciada con /consulta-licencia-*-init.
    """
    return await enviar_captcha_sesion_licencia(req.session_id, req.captcha_text)


@app.get("/licencia-captcha/{session_id}")
async def licencia_captcha_png(session_id: str):
    """
    Devuelve la imagen del captcha como PNG (útil para ver en Postman).
    """
    b64 = get_captcha_b64_sesion_licencia(session_id)
    try:
        raw = base64.b64decode(b64)
    except Exception:
        raise HTTPException(status_code=500, detail="Licencia: no se pudo decodificar la imagen captcha")
    return Response(
        content=raw,
        media_type="image/png",
        headers={"Content-Disposition": "inline; filename=licencia_captcha.png"},
    )


@app.post("/consulta-sunarp-licencia")
async def consulta_sunarp_mas_licencia(req: ConsultaRequest):
    """
    Hace consulta SUNARP y, con el primer propietario detectado, consulta licencia MTC.
    """
    browser = app.state.browser

    sunarp_res = await consulta_sunarp(req.placa, browser)

    # No hay propietarios detectados
    propietarios_det = sunarp_res.get("propietarios_detalle") or []
    if not propietarios_det:
        return {
            "ok": False,
            "mensaje": "SUNARP no devolvió propietarios para esta placa",
            "sunarp": sunarp_res,
        }

    dueño = propietarios_det[0]
    ap_pat = dueño.get("ap_paterno", "")
    ap_mat = dueño.get("ap_materno", "")
    nombres = dueño.get("nombres", "")

    if not ap_pat or not ap_mat or not nombres:
        return {
            "ok": False,
            "mensaje": "No se pudo extraer nombres completos del propietario",
            "propietario_detectado": dueño,
            "sunarp": sunarp_res,
        }

    licencia_res = await consulta_licencia_por_nombre(ap_pat, ap_mat, nombres, browser)

    return {
        "ok": True,
        "propietario_usado": dueño,
        "sunarp": sunarp_res,
        "licencia": licencia_res,
    }


# -------- SUTRAN (récord de infracciones) --------
@app.post("/consulta-sutran")
async def consulta_sutran_endpoint(req: ConsultaRequest):
    """
    Consulta de récord de infracciones Sutran por placa.
    """
    browser = app.state.browser
    return await consulta_sutran(req.placa.upper(), browser)


@app.post("/consulta-recompensas-nombre")
async def consulta_recompensas_nombre(req: RecompensasNombreRequest):
    browser = app.state.browser
    return await consulta_recompensas_por_nombre(req.nombre, browser)


@app.post("/consulta-sunarp-recompensas")
async def consulta_sunarp_recompensas(req: ConsultaRequest):
    browser = app.state.browser
    return await consulta_recompensas_desde_sunarp(req.placa, browser)


# -------- DNI por nombres --------
@app.post("/consulta-dni-nombres")
async def consulta_dni_nombres(req: DniNombreRequest):
    """
    Consulta buscardniperu.com por apellidos y nombres.
    """
    browser = app.state.browser
    return await consulta_dni_por_nombres(req.ap_paterno, req.ap_materno, req.nombres, browser)


@app.post("/consulta-dni-peru")
async def consulta_dni_peru_endpoint(req: DniPeruRequest):
    """
    Consulta dniperu.com para obtener nombres y apellidos por DNI.
    """
    browser = app.state.browser
    return await consulta_dni_peru(req.dni, browser)


@app.get("/health")
async def health():
    """
    Healthcheck simple.
    """
    return {"ok": True}


# -------- SAT Callao --------
@app.post("/consulta-sat-callao")
async def consulta_sat_callao_endpoint(req: ConsultaRequest):
    """
    Consulta de papeletas en pagopapeletascallao.pe.
    """
    browser = app.state.browser
    return await consulta_satcallao(req.placa.upper(), browser)


# -------- REDAM por DNI --------
@app.post("/consulta-redam-dni")
async def consulta_redam_dni_endpoint(req: RedamDniRequest):
    """
    Consulta en REDAM (casillas.pj.gob.pe/redam) por número de documento (DNI).
    """
    browser = app.state.browser
    return await consulta_redam_dni(req.dni, browser)
