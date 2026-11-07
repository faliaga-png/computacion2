"""
Script para las tres mediciones del README. Habla con la API por HTTP.

Uso (con la API ya levantada en localhost:8000):
    python mediciones/medir.py 1        # concurrencia I/O
    python mediciones/medir.py 2        # event loop bloqueado (correr con
                                        # USAR_EXECUTOR=false y después true)
    python mediciones/medir.py 3a       # techo del pool: cantidad de workers
    python mediciones/medir.py 3b       # techo del pool: núcleos de CPU
    python mediciones/medir.py 3c       # techo del pool: muchas tareas I/O
"""

import asyncio
import sys
import time

import httpx

API = "http://localhost:8000"

# 20 URLs que tardan ~1 segundo cada una en responder
URLS = [f"https://postman-echo.com/delay/1?n={i}" for i in range(20)]


async def crear(cliente, tipo, parametros, timeout=None):
    cuerpo = {"tipo": tipo, "parametros": parametros}
    if timeout:
        cuerpo["timeout"] = timeout
    respuesta = await cliente.post(f"{API}/tareas", json=cuerpo)
    respuesta.raise_for_status()
    return respuesta.json()["id"]


async def esperar_terminada(cliente, id_tarea):
    while True:
        tarea = (await cliente.get(f"{API}/tareas/{id_tarea}")).json()
        if tarea["estado"] not in ("pendiente", "ejecutando"):
            return tarea
        await asyncio.sleep(0.2)


async def lote(cliente, cantidad, tipo, parametros):
    """Crea 'cantidad' tareas a la vez y devuelve el tiempo total del lote."""
    ids = await asyncio.gather(*[crear(cliente, tipo, parametros, 600)
                                 for _ in range(cantidad)])
    terminadas = await asyncio.gather(*[esperar_terminada(cliente, i) for i in ids])
    # Se mide con los tiempos del servidor: desde que se creó la primera
    # hasta que terminó la última.
    total = max(t["fin"] for t in terminadas) - min(t["creada"] for t in terminadas)
    estados = {t["estado"] for t in terminadas}
    return total, estados


async def medicion_1(cliente):
    print("Medición 1: 20 descargas (cada URL tarda ~1s)\n")
    for concurrente in (False, True):
        id_tarea = await crear(cliente, "descargar",
                               {"urls": URLS, "concurrente": concurrente}, 120)
        tarea = await esperar_terminada(cliente, id_tarea)
        codigos = [r.get("codigo", r.get("error")) for r in tarea["resultado"]]
        modo = "concurrente" if concurrente else "secuencial "
        print(f"  {modo}: {tarea['duracion']:.2f} s   códigos: {set(codigos)}")


async def medicion_2(cliente):
    print("Medición 2: latencia de GET /tareas mientras corre un hash pesado\n")
    # Antes: latencia normal
    inicio = time.perf_counter()
    await cliente.get(f"{API}/tareas")
    print(f"  GET sin carga: {(time.perf_counter() - inicio) * 1000:.1f} ms")

    id_tarea = await crear(cliente, "hashear",
                           {"texto": "computacion2", "iteraciones": 30_000_000}, 120)
    await asyncio.sleep(0.2)  # darle tiempo a un worker a tomarla

    latencias = []
    while True:
        inicio = time.perf_counter()
        tarea = (await cliente.get(f"{API}/tareas/{id_tarea}")).json()
        await cliente.get(f"{API}/tareas")
        latencias.append((time.perf_counter() - inicio) * 1000)
        if tarea["estado"] != "ejecutando":
            break
        await asyncio.sleep(0.25)

    print(f"  duración del hash: {tarea['duracion']:.2f} s")
    print(f"  GETs durante el hash: {len(latencias)}")
    print(f"  latencia máxima:  {max(latencias):.1f} ms")
    print(f"  latencia promedio: {sum(latencias) / len(latencias):.1f} ms")


async def medicion_3a(cliente):
    print("Medición 3a: tareas 'esperar 1s' contra la cantidad de workers\n")
    stats = (await cliente.get(f"{API}/estadisticas")).json()
    print(f"  workers del servidor: {stats['workers_totales']}")
    for cantidad in (1, 2, 4, 8, 16, 32):
        total, estados = await lote(cliente, cantidad, "esperar", {"segundos": 1})
        print(f"  {cantidad:3d} tareas -> {total:6.2f} s  {estados}")


async def medicion_3b(cliente):
    print("Medición 3b: tareas 'hashear' (CPU) contra los núcleos\n")
    parametros = {"texto": "x", "iteraciones": 2_000_000}
    for cantidad in (1, 2, 4, 8, 10, 12, 16, 20, 30, 40):
        total, estados = await lote(cliente, cantidad, "hashear", parametros)
        print(f"  {cantidad:3d} hashes -> {total:6.2f} s  {estados}")


async def medicion_3c(cliente):
    print("Medición 3c: muchas tareas 'esperar 1s' con muchos workers\n")
    for cantidad in (100, 1000, 2000, 5000):
        total, estados = await lote(cliente, cantidad, "esperar", {"segundos": 1})
        print(f"  {cantidad:5d} tareas -> {total:6.2f} s  {estados}")


async def main():
    mediciones = {
        "1": medicion_1, "2": medicion_2,
        "3a": medicion_3a, "3b": medicion_3b, "3c": medicion_3c,
    }
    if len(sys.argv) != 2 or sys.argv[1] not in mediciones:
        print(__doc__)
        return
    limites = httpx.Limits(max_connections=200)
    async with httpx.AsyncClient(timeout=120, limits=limites) as cliente:
        await mediciones[sys.argv[1]](cliente)


if __name__ == "__main__":
    asyncio.run(main())
