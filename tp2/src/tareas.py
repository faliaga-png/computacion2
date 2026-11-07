"""
Los tres tipos de tarea que sabe ejecutar la plataforma.

- descargar: I/O-bound. Usa httpx en modo async.
- hashear:   CPU-bound. Es una función común (no async) porque se ejecuta
             en otro proceso a través de un executor.
- esperar:   I/O simulado con asyncio.sleep.
"""

import asyncio
import hashlib
import time

import httpx


# ---------------------------------------------------------------------------
# descargar (I/O-bound)
# ---------------------------------------------------------------------------

async def descargar_una(cliente, url):
    """Descarga una URL y devuelve código, tamaño y tiempo."""
    inicio = time.perf_counter()
    try:
        respuesta = await cliente.get(url)
        return {
            "url": url,
            "codigo": respuesta.status_code,
            "tamano_bytes": len(respuesta.content),
            "segundos": round(time.perf_counter() - inicio, 3),
        }
    except httpx.HTTPError as error:
        # Un error en una URL no hace fallar a toda la tarea:
        # se informa en el resultado de esa URL.
        return {
            "url": url,
            "error": f"{type(error).__name__}: {error}",
            "segundos": round(time.perf_counter() - inicio, 3),
        }


async def descargar(urls, concurrente=True):
    """
    Descarga una lista de URLs.

    concurrente=True  -> asyncio.gather: todas a la vez (tarda lo que la más lenta)
    concurrente=False -> un for con await: una detrás de otra (tarda la suma)

    El modo secuencial existe sólo para la medición 1 del README.
    """
    # "async with" garantiza que el cliente cierre sus conexiones
    # aunque la tarea se cancele a mitad de camino.
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as cliente:
        if concurrente:
            corrutinas = [descargar_una(cliente, url) for url in urls]
            resultados = await asyncio.gather(*corrutinas)
        else:
            resultados = []
            for url in urls:
                resultados.append(await descargar_una(cliente, url))
    return resultados


# ---------------------------------------------------------------------------
# hashear (CPU-bound)
# ---------------------------------------------------------------------------

def hashear(texto, iteraciones):
    """
    Aplica SHA-256 'iteraciones' veces.

    Es una función normal, NO una corrutina: no tiene ningún await,
    así que si se llamara directo desde el event loop lo bloquearía
    hasta terminar.
    """
    datos = texto.encode()
    for _ in range(iteraciones):
        datos = hashlib.sha256(datos).digest()
    return {"iteraciones": iteraciones, "hash": datos.hex()}


# ---------------------------------------------------------------------------
# esperar (I/O simulado)
# ---------------------------------------------------------------------------

async def esperar(segundos):
    """Duerme sin bloquear: asyncio.sleep le devuelve el control al loop."""
    await asyncio.sleep(segundos)
    return {"dormido_segundos": segundos}


# ---------------------------------------------------------------------------
# Validación de parámetros (se hace en el POST, antes de encolar)
# ---------------------------------------------------------------------------

TIPOS_VALIDOS = ["descargar", "hashear", "esperar"]


def validar_parametros(tipo, parametros):
    """Devuelve un mensaje de error, o None si los parámetros están bien."""
    if tipo not in TIPOS_VALIDOS:
        return f"tipo inválido, debe ser uno de {TIPOS_VALIDOS}"

    if tipo == "descargar":
        urls = parametros.get("urls")
        if not isinstance(urls, list) or len(urls) == 0:
            return "descargar necesita 'urls': una lista no vacía"
        for url in urls:
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                return f"url inválida: {url}"

    if tipo == "hashear":
        if not isinstance(parametros.get("texto"), str):
            return "hashear necesita 'texto' (string)"
        iteraciones = parametros.get("iteraciones")
        if not isinstance(iteraciones, int) or iteraciones < 1:
            return "hashear necesita 'iteraciones' (entero mayor a 0)"

    if tipo == "esperar":
        segundos = parametros.get("segundos")
        if not isinstance(segundos, (int, float)) or segundos < 0:
            return "esperar necesita 'segundos' (número no negativo)"

    return None
