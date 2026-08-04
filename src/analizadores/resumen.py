"""
Analizador: Resumen
--------------------
Corre como PROCESO independiente (no thread). Cada `intervalo` segundos
relee el estado de todos los PIDs vivos y publica el resultado en la
memoria compartida (snapshot['resumen']).

Por qué proceso y no thread: esto es CPU-bound (parsear texto de `ps`,
formatear diccionarios) y además necesitamos que si este analizador se
cuelga o lo matamos con `kill`, el resto del monitor siga funcionando.
Con threads, un cuelgue acá podría arrastrar al proceso completo por
compartir el mismo intérprete (y el GIL sigue serializando ese trabajo
igual). Con procesos separados, cada analizador es una unidad de fallo
aislada.
"""
import time
import macos_api


def correr(snapshot, pids_compartidos, intervalo, stop_event, verbose=None):
    while not stop_event.is_set():
        t0 = time.time()
        try:
            procesos = macos_api.ps_snapshot()
            pids_compartidos[:] = list(procesos.keys())
            snapshot["resumen"]["data"] = procesos
            snapshot["resumen"]["ts"] = time.time()
        except Exception as e:
            snapshot["resumen"]["error"] = str(e)

        espera = max(0.05, intervalo.value - (time.time() - t0))
        stop_event.wait(espera)
