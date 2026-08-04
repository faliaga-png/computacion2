"""
Analizador: File Descriptors
------------------------------
A diferencia de Resumen/Memoria (que barren TODOS los procesos), esta
vista es cara de pedir en masa (lsof por cada PID sería un fork+exec
por proceso). Por eso solo inspeccionamos el proceso "en foco" (el
seleccionado/pineado en la TUI), igual que hace Memoria con vmmap.

Esto es una decision de diseno explicita: preferimos una vista de
detalle rapida para UN proceso antes que una vista global lenta y que
trabe el resto del monitor.
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

            limite = 128 if (verbose is not None and verbose.value) else 32
            fds = macos_api.fds_de(foco, limite=limite) if foco else []

            snapshot["fds"]["data"] = {"pid_foco": foco, "fds": fds}
            snapshot["fds"]["ts"] = time.time()
        except Exception as e:
            snapshot["fds"]["error"] = str(e)

        espera = max(0.2, intervalo.value - (time.time() - t0))
        stop_event.wait(espera)
