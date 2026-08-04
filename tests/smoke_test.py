import sys, os, time, signal as sig
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "analizadores"))
import multiprocessing as mp

import recolector, manejador_senales
import resumen as an_resumen
import memoria as an_memoria
import fds as an_fds
import threads as an_threads
import senales as an_senales
import scheduling as an_scheduling
import sistema as an_sistema

VISTAS = ["resumen", "memoria", "fds", "threads", "senales", "scheduling", "sistema"]
ANALIZADORES = {
    "resumen": an_resumen.correr, "memoria": an_memoria.correr, "fds": an_fds.correr,
    "threads": an_threads.correr, "senales": an_senales.correr,
    "scheduling": an_scheduling.correr, "sistema": an_sistema.correr,
}

r_fd = manejador_senales.instalar()

with mp.Manager() as manager:
    snapshot = manager.dict()
    for v in VISTAS:
        snapshot[v] = manager.dict({"data": {}, "ts": 0.0})
    snapshot["_foco"] = manager.dict({"pid": os.getpid()})  # nos auto-observamos

    pids = manager.list()
    stop_event = mp.Event()
    verbose = mp.Value("i", 0)
    intervalos = {v: mp.Value("d", 0.5) for v in VISTAS}

    hijos = []
    p = mp.Process(target=recolector.correr, args=(snapshot, pids, stop_event), daemon=True)
    p.start(); hijos.append(p)
    for v in VISTAS:
        p = mp.Process(target=ANALIZADORES[v], args=(snapshot, pids, intervalos[v], stop_event, verbose), daemon=True)
        p.start(); hijos.append(p)

    time.sleep(3)

    print("--- pids vivos detectados:", len(pids))
    for v in VISTAS:
        d = dict(snapshot[v])
        print(f"--- {v}: ts={d.get('ts')} error={d.get('error')} keys_data={list(d.get('data', {}))[:3]}")

    # probamos el self-pipe: nos mandamos SIGUSR2 a nosotros mismos
    os.kill(os.getpid(), sig.SIGUSR2)
    time.sleep(0.2)
    pendientes = manejador_senales.leer_pendientes(r_fd)
    ctx = {"stop_event": stop_event, "intervalos": intervalos, "snapshot": snapshot,
           "verbose": verbose, "config_path": "config.json"}
    for n in pendientes:
        print("--- senal recibida:", n, "->", manejador_senales.atender(n, ctx))
    print("--- verbose.value tras SIGUSR2:", verbose.value)

    stop_event.set()
    for p in hijos:
        p.join(timeout=2)
    print("--- shutdown OK")
