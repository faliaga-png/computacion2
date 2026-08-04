"""
main.py
--------
Punto de entrada. Se ejecuta con: python3 src/main.py

Arma la memoria compartida (multiprocessing.Manager), lanza el
recolector y los 7 analizadores como procesos independientes, instala
el manejo de señales (self-pipe) y arranca la TUI en el proceso
principal.

NOTA IMPORTANTE sobre multiprocessing en macOS:
Desde Python 3.8, el metodo de arranque por defecto en macOS es
'spawn', no 'fork' (que sigue siendo el default en Linux). Con spawn,
cada proceso hijo arranca un interprete de Python NUEVO Y VACIO, y
vuelve a importar este módulo (main.py) desde cero antes de ejecutar
la función que se le paso como target. Por eso:
  1) Todo el código de "armado" (imports, sys.path) está a nivel de
     módulo, para que se repita en cada hijo.
  2) El código que CREA los procesos está protegido por
     `if __name__ == "__main__":` — si no lo estuviera, cada hijo
     intentaría crear sus propios hijos recursivamente al re-importar
     este archivo (bomba de fork/spawn infinita).
Con fork (Linux) los hijos heredan una copia del proceso padre ya
inicializado (copy-on-write) y esto no hace falta; con spawn, sí.
"""
import sys
import os
import json
import multiprocessing as mp
import curses

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "analizadores"))

import macos_api
import recolector
import manejador_senales
import display

import resumen as an_resumen
import memoria as an_memoria
import fds as an_fds
import threads as an_threads
import senales as an_senales
import scheduling as an_scheduling
import sistema as an_sistema

VISTAS = ["resumen", "memoria", "fds", "threads", "senales", "scheduling", "sistema"]
ANALIZADORES = {
    "resumen": an_resumen.correr,
    "memoria": an_memoria.correr,
    "fds": an_fds.correr,
    "threads": an_threads.correr,
    "senales": an_senales.correr,
    "scheduling": an_scheduling.correr,
    "sistema": an_sistema.correr,
}

CONFIG_PATH = os.path.join(os.path.dirname(BASE_DIR), "config.json")


def cargar_config():
    default = {
        "intervals": {"resumen": 2.0, "memoria": 3.0, "fds": 5.0, "threads": 2.0,
                       "senales": 10.0, "scheduling": 10.0, "sistema": 2.0},
        "min_intervals": {"resumen": 0.5, "memoria": 1.0, "fds": 2.0, "threads": 0.5,
                           "senales": 5.0, "scheduling": 5.0, "sistema": 1.0},
    }
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        default["intervals"].update(cfg.get("intervals", {}))
        default["min_intervals"].update(cfg.get("min_intervals", {}))
    except Exception:
        pass
    return default


def main():
    config = cargar_config()

    # 1) Instalamos el manejo de senales ANTES de crear hijos: asi el
    #    signal.set_wakeup_fd (que setea un fd global de este proceso)
    #    queda establecido en el padre y los hijos, al hacer spawn,
    #    arrancan con su propio estado de señales por defecto (no
    #    heredan el pipe), que es justo lo que queremos: solo el padre
    #    (donde corre la TUI) reacciona a las señales del monitor.
    r_fd_senales = manejador_senales.instalar()

    with mp.Manager() as manager:
        snapshot = manager.dict()
        for vista in VISTAS:
            snapshot[vista] = manager.dict({"data": {}, "ts": 0.0})
        snapshot["_foco"] = manager.dict({"pid": None})

        pids_compartidos = manager.list()
        stop_event = mp.Event()
        verbose = mp.Value("i", 0)

        intervalos = {
            vista: mp.Value("d", config["intervals"][vista]) for vista in VISTAS
        }

        procesos_hijos = []

        p_recolector = mp.Process(
            target=recolector.correr,
            args=(snapshot, pids_compartidos, stop_event),
            daemon=True, name="recolector",
        )
        p_recolector.start()
        procesos_hijos.append(p_recolector)

        for vista in VISTAS:
            p = mp.Process(
                target=ANALIZADORES[vista],
                args=(snapshot, pids_compartidos, intervalos[vista], stop_event, verbose),
                daemon=True, name=f"analizador-{vista}",
            )
            p.start()
            procesos_hijos.append(p)

        try:
            curses.wrapper(
                display.correr, snapshot, intervalos, pids_compartidos,
                stop_event, verbose, CONFIG_PATH, r_fd_senales,
                config["min_intervals"],
            )
        finally:
            # Shutdown limpio: avisamos a todos los hijos y esperamos
            # un poco antes de forzar el join, para que cada analizador
            # termine su iteración actual en vez de cortarse a mitad de
            # una escritura al snapshot compartido.
            stop_event.set()
            for p in procesos_hijos:
                p.join(timeout=2)
            for p in procesos_hijos:
                if p.is_alive():
                    p.terminate()


if __name__ == "__main__":
    main()
