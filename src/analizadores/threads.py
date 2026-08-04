"""
Analizador: Threads
---------------------
Igual que FDs: mostrar los threads de TODOS los procesos en cada tick
seria carisimo (un `ps -M` por PID). Mostramos los threads del proceso
en foco, mas la CANTIDAD de threads por proceso (que ya viene gratis
en ps_snapshot via la columna nlwp) para la lista general.
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

            threads = macos_api.threads_de(foco) if foco else []

            snapshot["threads"]["data"] = {"pid_foco": foco, "threads": threads}
            snapshot["threads"]["ts"] = time.time()
        except Exception as e:
            snapshot["threads"]["error"] = str(e)

        espera = max(0.2, intervalo.value - (time.time() - t0))
        stop_event.wait(espera)
