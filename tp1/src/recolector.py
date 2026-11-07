"""
Recolector
-----------
Es el proceso mas simple de todos, pero conceptualmente el "raiz" de
la arquitectura: cada 1 segundo actualiza la lista de PIDs vivos en
memoria compartida (pids_compartidos). El resto de los analizadores
usan `ps_snapshot()` propio para sus datos (porque cada uno necesita
columnas distintas), pero el recolector es la referencia de "quien
sigue vivo" para que el display pueda, por ejemplo, invalidar un PID
pineado que ya no existe.
"""
import time
import macos_api


def correr(snapshot, pids_compartidos, stop_event):
    while not stop_event.is_set():
        try:
            vivos = macos_api.list_pids()
            pids_compartidos[:] = vivos

            # Si el proceso en foco murio, lo soltamos.
            try:
                foco = snapshot["_foco"]["pid"]
                if foco and foco not in vivos:
                    snapshot["_foco"]["pid"] = None
            except Exception:
                pass
        except Exception:
            pass
        stop_event.wait(1.0)
