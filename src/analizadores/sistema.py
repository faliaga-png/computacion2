"""
Analizador: Sistema
----------------------
El unico analizador que no depende de un "foco": siempre calcula stats
globales (CPU, memoria, load average, swap, uptime) y ademas deriva el
top-3 por CPU y por RSS a partir del ultimo snapshot de resumen (que ya
esta en memoria compartida, no hace falta re-pedirselo a `ps`).

El desglose de CPU global (user/system/idle %) se calcula con un DELTA
entre dos lecturas de `kern.cp_time` (contador acumulativo de ticks),
igual que se calcula el %CPU de un proceso individual con los campos
utime/stime de /proc/<pid>/stat en la version Linux del TP. Por eso
este analizador guarda el ultimo muestreo en una variable LOCAL al loop
(no en memoria compartida: solo este proceso necesita el valor previo).
"""
import time
import macos_api


def correr(snapshot, pids_compartidos, intervalo, stop_event, verbose=None):
    ticks_previos = macos_api.cpu_global_ticks()

    while not stop_event.is_set():
        t0 = time.time()
        try:
            info = macos_api.sistema_snapshot()

            ticks_actuales = macos_api.cpu_global_ticks()
            info["cpu_global_pct"] = _delta_cpu_pct(ticks_previos, ticks_actuales)
            if ticks_actuales is not None:
                ticks_previos = ticks_actuales

            try:
                procesos = dict(snapshot["resumen"]["data"])
            except Exception:
                procesos = {}

            info["total_procesos"] = len(procesos)
            info["por_estado"] = _contar_estados(procesos)
            info["total_threads"] = sum(p.get("nthreads", 0) for p in procesos.values())
            info["top_cpu"] = _top(procesos, "cpu_pct", 3)
            info["top_mem"] = _top(procesos, "rss_kb", 3)

            snapshot["sistema"]["data"] = info
            snapshot["sistema"]["ts"] = time.time()
        except Exception as e:
            snapshot["sistema"]["error"] = str(e)

        espera = max(0.2, intervalo.value - (time.time() - t0))
        stop_event.wait(espera)


def _delta_cpu_pct(previos, actuales):
    if not previos or not actuales:
        return {"user": 0.0, "system": 0.0, "idle": 0.0}
    d_user = actuales["user"] - previos["user"]
    d_nice = actuales["nice"] - previos["nice"]
    d_system = actuales["system"] - previos["system"]
    d_idle = actuales["idle"] - previos["idle"]
    total = d_user + d_nice + d_system + d_idle
    if total <= 0:
        return {"user": 0.0, "system": 0.0, "idle": 0.0}
    return {
        "user": round(100.0 * (d_user + d_nice) / total, 1),
        "system": round(100.0 * d_system / total, 1),
        "idle": round(100.0 * d_idle / total, 1),
    }


def _contar_estados(procesos):
    conteo = {}
    for p in procesos.values():
        e = p.get("estado", "?")
        conteo[e] = conteo.get(e, 0) + 1
    return conteo


def _top(procesos, campo, n):
    ordenados = sorted(procesos.values(), key=lambda p: p.get(campo, 0), reverse=True)
    return [
        {"pid": p["pid"], "comando": p.get("comando", ""), campo: p.get(campo, 0)}
        for p in ordenados[:n]
    ]
