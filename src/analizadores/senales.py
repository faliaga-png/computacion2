"""
Analizador: Senales
---------------------
OJO: esto analiza las senales PENDIENTES/BLOQUEADAS de los procesos que
el monitor esta observando (columna "sig"/"sigmask" de ps para el PID
en foco). No confundir con manejador_senales.py, que es el codigo que
maneja las senales que le llegan al MONITOR mismo (SIGINT, SIGHUP, etc).
Son dos cosas conceptualmente distintas aunque el nombre se parezca.
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

            info = macos_api.senales_de(foco) if foco else {
                "pendientes": [], "bloqueadas": [], "handler": "sin proceso en foco"
            }

            snapshot["senales"]["data"] = {"pid_foco": foco, **info}
            snapshot["senales"]["ts"] = time.time()
        except Exception as e:
            snapshot["senales"]["error"] = str(e)

        espera = max(0.5, intervalo.value - (time.time() - t0))
        stop_event.wait(espera)
