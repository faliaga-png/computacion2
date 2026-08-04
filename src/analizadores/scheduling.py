"""
Analizador: Scheduling
------------------------
Trae nice/priority/pgid/sid del proceso en foco, mas la explicacion de
las dos limitaciones reales de macOS frente a Linux: no hay CPU affinity
para procesos sin privilegios, y QoS class no es facil de leer desde
afuera sin instrumentacion privada (Instruments/dtrace con privilegios).
"""
import time
import macos_api


def correr(snapshot, pids_compartidos, intervalo, stop_event, verbose=None):
    while not stop_event.is_set():
        t0 = time.time()
        try:
            try:
                foco = snapshot["_foco"]["pid"]
            except Exception:
                foco = None

            info = macos_api.scheduling_de(foco) if foco else {}

            snapshot["scheduling"]["data"] = {"pid_foco": foco, **info}
            snapshot["scheduling"]["ts"] = time.time()
        except Exception as e:
            snapshot["scheduling"]["error"] = str(e)

        espera = max(0.5, intervalo.value - (time.time() - t0))
        stop_event.wait(espera)
