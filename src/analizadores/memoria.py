"""
Analizador: Memoria
--------------------
Usa lo que ya trae `ps_snapshot` (RSS/VSZ, que es barato) para TODOS los
procesos, y además le pide a `vmmap` el detalle de regiones SOLO del
proceso actualmente seleccionado/pineado en la TUI (eso es carísimo de
pedir para todos los procesos en cada tick, así que lo restringimos).

El "foco" (qué PID mirar en detalle) viaja desde el display hacia acá
por memoria compartida: snapshot['_foco']['pid'].
"""
import time
import macos_api


def correr(snapshot, pids_compartidos, intervalo, stop_event, verbose=None):
    while not stop_event.is_set():
        t0 = time.time()
        try:
            procesos = macos_api.ps_snapshot()
            resumen_mem = {
                pid: {
                    "rss_kb": d["rss_kb"],
                    "vsz_kb": d["vsz_kb"],
                }
                for pid, d in procesos.items()
            }

            detalle = {}
            try:
                foco = snapshot["_foco"]["pid"]
            except Exception:
                foco = None
            if foco and foco in procesos:
                detalle = macos_api.memoria_detalle(foco)

            snapshot["memoria"]["data"] = {"por_proceso": resumen_mem, "detalle_foco": detalle,
                                            "pid_foco": foco}
            snapshot["memoria"]["ts"] = time.time()
        except Exception as e:
            snapshot["memoria"]["error"] = str(e)

        espera = max(0.1, intervalo.value - (time.time() - t0))
        stop_event.wait(espera)
