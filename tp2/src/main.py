"""
API HTTP de la plataforma de tareas distribuidas.

Se ejecuta con:  python src/main.py
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from plataforma import ESTADOS, Plataforma
from tareas import validar_parametros

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

# Configuración por variables de entorno (se cambian en docker-compose.yml)
plataforma = Plataforma(
    cant_workers=int(os.getenv("CANT_WORKERS", "4")),
    timeout_por_defecto=float(os.getenv("TIMEOUT_POR_DEFECTO", "60")),
    usar_executor=os.getenv("USAR_EXECUTOR", "true").lower() == "true",
    tamano_cola=int(os.getenv("TAMANO_COLA", "1000")),
    limite_apagado=float(os.getenv("LIMITE_APAGADO", "10")),
    directorio_volcados=os.getenv("DIRECTORIO_VOLCADOS", "volcados"),
)


@asynccontextmanager
async def lifespan(app):
    # Lo que está antes del yield corre al arrancar el servidor,
    # lo que está después corre al apagarlo (uvicorn llega acá cuando
    # recibe SIGINT o SIGTERM).
    await plataforma.iniciar()
    yield
    await plataforma.apagar()


app = FastAPI(title="Plataforma de tareas distribuidas", lifespan=lifespan)


class NuevaTarea(BaseModel):
    tipo: str
    parametros: dict = {}
    timeout: float | None = None


# Todos los endpoints son "async def" a propósito: así corren en el event
# loop, igual que los workers. Si fueran "def", FastAPI los correría en otro
# thread y ya no podríamos tocar el estado compartido sin Lock.

@app.post("/tareas", status_code=202)
async def crear_tarea(pedido: NuevaTarea):
    if not plataforma.aceptando:
        raise HTTPException(503, "el servidor se está apagando")

    error = validar_parametros(pedido.tipo, pedido.parametros)
    if error:
        raise HTTPException(400, error)
    if pedido.timeout is not None and pedido.timeout <= 0:
        raise HTTPException(400, "timeout debe ser mayor a 0")

    try:
        tarea = plataforma.encolar(pedido.tipo, pedido.parametros, pedido.timeout)
    except asyncio.QueueFull:
        raise HTTPException(503, "la cola está llena, probá más tarde")

    # Se responde enseguida: la tarea la ejecuta un worker después.
    return {"id": tarea["id"], "estado": tarea["estado"]}


@app.get("/tareas")
async def listar_tareas(estado: str | None = None):
    if estado is not None and estado not in ESTADOS:
        raise HTTPException(400, f"estado inválido, debe ser uno de {ESTADOS}")
    return plataforma.listar(estado)


@app.get("/tareas/{id_tarea}")
async def ver_tarea(id_tarea: str):
    tarea = plataforma.obtener(id_tarea)
    if tarea is None:
        raise HTTPException(404, "no existe esa tarea")
    return tarea


@app.delete("/tareas/{id_tarea}")
async def cancelar_tarea(id_tarea: str):
    tarea = plataforma.obtener(id_tarea)
    if tarea is None:
        raise HTTPException(404, "no existe esa tarea")
    if not plataforma.cancelar(id_tarea):
        raise HTTPException(409, f"no se puede cancelar: ya está {tarea['estado']}")
    return {"id": id_tarea, "estado": tarea["estado"]}


@app.get("/estadisticas")
async def estadisticas():
    return plataforma.estadisticas()


# Guarda obligatoria: en macOS/Windows el ProcessPoolExecutor arranca los
# procesos hijos con "spawn", que vuelve a importar este archivo. Sin el if,
# cada hijo levantaría otro servidor.
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PUERTO", "8000")))
