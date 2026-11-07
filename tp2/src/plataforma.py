"""
El núcleo del sistema: la cola, el pool de workers y el estado de las tareas.

Todo esto corre en UN solo hilo (el del event loop). Por eso no hace falta
ningún Lock: el loop sólo cambia de corrutina en los await, así que entre
dos await nadie más puede tocar 'self.tareas' a la mitad.
"""

import asyncio
import glob
import json
import logging
import os
import time
import uuid
from concurrent.futures import ProcessPoolExecutor

import tareas

log = logging.getLogger("plataforma")

ESTADOS = ["pendiente", "ejecutando", "completada", "fallida", "cancelada"]


class Plataforma:

    def __init__(self, cant_workers, timeout_por_defecto, usar_executor,
                 tamano_cola, limite_apagado, directorio_volcados):
        self.cant_workers = cant_workers
        self.timeout_por_defecto = timeout_por_defecto
        self.usar_executor = usar_executor
        self.tamano_cola = tamano_cola
        self.limite_apagado = limite_apagado
        self.directorio_volcados = directorio_volcados

        # id -> diccionario con los datos de la tarea (lo que ve el cliente)
        self.tareas = {}
        # id -> asyncio.Task de las tareas que se están ejecutando ahora
        self.en_ejecucion = {}
        # ids de tareas que el usuario (o el apagado) pidió cancelar
        self.cancelaciones_pedidas = set()

        self.cola = None
        self.workers = []
        self.executor = None
        self.aceptando = False

    # ------------------------------------------------------------------
    # Arranque y apagado
    # ------------------------------------------------------------------

    async def iniciar(self):
        self.reportar_volcado_previo()

        self.cola = asyncio.Queue(maxsize=self.tamano_cola)

        if self.usar_executor:
            # Procesos y no threads: por el GIL, dos threads de Python no
            # pueden calcular hashes al mismo tiempo. Con procesos sí.
            self.executor = ProcessPoolExecutor()
        else:
            log.warning("USAR_EXECUTOR=false: 'hashear' va a BLOQUEAR el event loop "
                        "(sólo para la medición 2)")

        for numero in range(self.cant_workers):
            self.workers.append(asyncio.create_task(self.worker(numero)))

        self.aceptando = True
        log.info("Plataforma iniciada con %d workers", self.cant_workers)

    async def apagar(self):
        """Apagado ordenado: lo llama la API al recibir SIGTERM o SIGINT."""
        log.info("Apagando: no se aceptan más tareas")
        self.aceptando = False

        # 1. Las que siguen en la cola no llegaron a empezar: se cancelan.
        while not self.cola.empty():
            id_tarea = self.cola.get_nowait()
            self.cola.task_done()
            tarea = self.tareas[id_tarea]
            if tarea["estado"] == "pendiente":
                tarea["estado"] = "cancelada"
                tarea["error"] = "cancelada por apagado del servidor"
                tarea["fin"] = time.time()

        # 2. Esperar a las que están en ejecución, con un límite de tiempo.
        #    cola.join() termina cuando todos los workers llamaron task_done().
        if self.en_ejecucion:
            log.info("Esperando hasta %ss a %d tareas en curso",
                     self.limite_apagado, len(self.en_ejecucion))
        try:
            await asyncio.wait_for(self.cola.join(), timeout=self.limite_apagado)
        except asyncio.TimeoutError:
            # 3. Se acabó el tiempo: cancelar las que quedan.
            log.warning("Se venció el límite, cancelando %d tareas",
                        len(self.en_ejecucion))
            for id_tarea, tarea_async in list(self.en_ejecucion.items()):
                self.cancelaciones_pedidas.add(id_tarea)
                self.tareas[id_tarea]["error"] = "cancelada por apagado del servidor"
                tarea_async.cancel()
            await self.cola.join()

        # 4. Los workers ya están libres (esperando en cola.get()): se cancelan.
        for w in self.workers:
            w.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)

        if self.executor is not None:
            # shutdown(wait=True) es bloqueante: se lo manda a un thread
            # para que el event loop no se congele mientras los procesos cierran.
            await asyncio.to_thread(self.executor.shutdown, wait=True,
                                    cancel_futures=True)

        self.volcar_estado()
        log.info("Apagado completo")

    # ------------------------------------------------------------------
    # Operaciones que usa la API
    # ------------------------------------------------------------------

    def encolar(self, tipo, parametros, timeout):
        """Crea la tarea y la pone en la cola. No espera a que se ejecute."""
        id_tarea = uuid.uuid4().hex[:8]
        tarea = {
            "id": id_tarea,
            "tipo": tipo,
            "parametros": parametros,
            "timeout": timeout or self.timeout_por_defecto,
            "estado": "pendiente",
            "creada": time.time(),
            "inicio": None,
            "fin": None,
            "duracion": None,
            "worker": None,
            "resultado": None,
            "error": None,
        }
        # put_nowait lanza asyncio.QueueFull si la cola está llena,
        # y la API lo convierte en un 503.
        self.cola.put_nowait(id_tarea)
        self.tareas[id_tarea] = tarea
        return tarea

    def listar(self, estado=None):
        if estado is None:
            return list(self.tareas.values())
        return [t for t in self.tareas.values() if t["estado"] == estado]

    def obtener(self, id_tarea):
        return self.tareas.get(id_tarea)

    def cancelar(self, id_tarea):
        """
        Devuelve True si se pudo cancelar, False si la tarea ya había terminado.
        """
        tarea = self.tareas[id_tarea]

        if tarea["estado"] == "pendiente":
            # Todavía está en la cola: la marcamos y el worker la va a saltear.
            tarea["estado"] = "cancelada"
            tarea["fin"] = time.time()
            return True

        if tarea["estado"] == "ejecutando":
            self.cancelaciones_pedidas.add(id_tarea)
            # cancel() hace que en el próximo await de la tarea se lance
            # CancelledError. Devuelve False si la tarea ya había terminado.
            if self.en_ejecucion[id_tarea].cancel():
                tarea["estado"] = "cancelada"
                return True
            return False

        return False

    def estadisticas(self):
        por_estado = {}
        for estado in ESTADOS:
            por_estado[estado] = 0
        for tarea in self.tareas.values():
            por_estado[tarea["estado"]] += 1

        # Duración promedio de las completadas, por tipo
        duracion_promedio = {}
        for tipo in tareas.TIPOS_VALIDOS:
            duraciones = [t["duracion"] for t in self.tareas.values()
                          if t["tipo"] == tipo and t["estado"] == "completada"]
            if duraciones:
                duracion_promedio[tipo] = round(sum(duraciones) / len(duraciones), 3)

        # Cuánto esperaron en la cola antes de que un worker las tomara
        esperas = [t["inicio"] - t["creada"] for t in self.tareas.values()
                   if t["inicio"] is not None]
        espera_promedio = round(sum(esperas) / len(esperas), 3) if esperas else None

        return {
            "total": len(self.tareas),
            "por_estado": por_estado,
            "duracion_promedio_segundos": duracion_promedio,
            "espera_promedio_en_cola_segundos": espera_promedio,
            "workers_totales": self.cant_workers,
            "workers_ocupados": len(self.en_ejecucion),
            "tareas_en_cola": self.cola.qsize(),
        }

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    async def worker(self, numero):
        """
        Consumidor: saca ids de la cola y ejecuta las tareas, para siempre.
        Termina sólo cuando el apagado lo cancela.
        """
        while True:
            id_tarea = await self.cola.get()
            try:
                tarea = self.tareas[id_tarea]
                if tarea["estado"] == "cancelada":
                    continue  # la cancelaron mientras estaba en la cola
                await self.ejecutar(tarea, numero)
            except Exception:
                # Red de seguridad: si algo falla acá, el worker NO se muere.
                log.exception("Error inesperado en el worker %d", numero)
            finally:
                self.cola.task_done()

    async def ejecutar(self, tarea, numero):
        id_tarea = tarea["id"]
        tarea["estado"] = "ejecutando"
        tarea["inicio"] = time.time()
        tarea["worker"] = numero

        # La tarea corre en su propia asyncio.Task para poder cancelarla
        # desde el DELETE sin cancelar al worker. wait_for le pone el timeout.
        tarea_async = asyncio.create_task(
            asyncio.wait_for(self.correr_segun_tipo(tarea), tarea["timeout"])
        )
        self.en_ejecucion[id_tarea] = tarea_async

        try:
            tarea["resultado"] = await tarea_async
            tarea["estado"] = "completada"
        except asyncio.TimeoutError:
            tarea["estado"] = "fallida"
            tarea["error"] = f"timeout: superó los {tarea['timeout']} segundos"
        except asyncio.CancelledError:
            tarea["estado"] = "cancelada"
            if id_tarea not in self.cancelaciones_pedidas:
                # No la canceló nadie desde la API: cancelaron al worker mismo.
                # Hay que dejar que el CancelledError siga subiendo.
                raise
        except Exception as error:
            # La tarea lanzó una excepción: queda fallida y el worker sigue.
            tarea["estado"] = "fallida"
            tarea["error"] = f"{type(error).__name__}: {error}"
        finally:
            tarea["fin"] = time.time()
            tarea["duracion"] = round(tarea["fin"] - tarea["inicio"], 3)
            del self.en_ejecucion[id_tarea]
            self.cancelaciones_pedidas.discard(id_tarea)

    async def correr_segun_tipo(self, tarea):
        parametros = tarea["parametros"]

        if tarea["tipo"] == "descargar":
            return await tareas.descargar(parametros["urls"],
                                          parametros.get("concurrente", True))

        if tarea["tipo"] == "esperar":
            return await tareas.esperar(parametros["segundos"])

        if tarea["tipo"] == "hashear":
            if self.usar_executor:
                # Bien: el cálculo va a otro proceso y el loop queda libre.
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(
                    self.executor, tareas.hashear,
                    parametros["texto"], parametros["iteraciones"])
            # Mal a propósito (medición 2): se llama directo y bloquea el loop.
            return tareas.hashear(parametros["texto"], parametros["iteraciones"])

        raise ValueError(f"tipo desconocido: {tarea['tipo']}")

    # ------------------------------------------------------------------
    # Persistencia mínima
    # ------------------------------------------------------------------

    def volcar_estado(self):
        os.makedirs(self.directorio_volcados, exist_ok=True)
        nombre = f"estado_{time.strftime('%Y%m%d_%H%M%S')}.json"
        ruta = os.path.join(self.directorio_volcados, nombre)
        with open(ruta, "w") as archivo:
            json.dump(list(self.tareas.values()), archivo, indent=2, default=str)
        log.info("Estado volcado en %s (%d tareas)", ruta, len(self.tareas))

    def reportar_volcado_previo(self):
        patron = os.path.join(self.directorio_volcados, "estado_*.json")
        volcados = sorted(glob.glob(patron))
        if not volcados:
            log.info("No hay volcados previos en %s", self.directorio_volcados)
            return

        ultimo = volcados[-1]
        try:
            with open(ultimo) as archivo:
                tareas_previas = json.load(archivo)
        except (OSError, json.JSONDecodeError) as error:
            log.warning("Hay un volcado previo (%s) pero no se pudo leer: %s",
                        ultimo, error)
            return

        conteo = {}
        for tarea in tareas_previas:
            conteo[tarea["estado"]] = conteo.get(tarea["estado"], 0) + 1
        log.info("Volcado previo encontrado: %s con %d tareas %s",
                 ultimo, len(tareas_previas), conteo)
