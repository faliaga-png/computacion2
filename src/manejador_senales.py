"""
manejador_senales.py
----------------------
Maneja las senales que le llegan al MONITOR (no a los procesos que
observa: eso es analizadores/senales.py, otra cosa).

Por que self-pipe / set_wakeup_fd:
Un signal handler de Python corre "en medio" del programa, en un punto
que el interprete decide, y solo puede llamar a funciones que sean
async-signal-safe (basicamente: nada que asigne memoria de forma
compleja, nada que tome locks). Si desde el handler tocamos
directamente un Manager.dict() (que por debajo hace IPC de red hacia
el proceso servidor del Manager), corremos riesgo de deadlock si la
senal interrumpe justo en medio de esa comunicacion.

La solucion clasica (self-pipe trick) es: el handler NO hace nada mas
que "despertar" el loop principal escribiendo un byte en un pipe.
signal.set_wakeup_fd() hace esto automaticamente por nosotros: cuando
llega una senal, Python escribe el NUMERO de senal (1 byte) al fd que
le indiquemos, sin que nuestro handler tenga que hacer nada riesgoso.
El loop principal (la TUI) despues lee ese pipe de forma NO bloqueante,
en un punto seguro de su propio ciclo, y ahi si actua con calma:
recargar config, volcar el snapshot, etc.
"""
import os
import signal
import json
import time


SENALES_MONITOREADAS = [
    signal.SIGINT,
    signal.SIGTERM,
    signal.SIGHUP,
    signal.SIGUSR1,
    signal.SIGUSR2,
    signal.SIGWINCH,
]


def instalar():
    """
    Crea el pipe, lo conecta con set_wakeup_fd, e instala handlers
    vacios (no-op) para que el SO no aplique la accion por defecto
    (que para SIGINT/SIGTERM seria matar el proceso sin avisar).
    Devuelve el file descriptor de lectura, para que el loop principal
    lo consulte de forma no bloqueante.
    """
    r_fd, w_fd = os.pipe()
    os.set_blocking(r_fd, False)
    os.set_blocking(w_fd, False)
    signal.set_wakeup_fd(w_fd)

    def _noop(signum, frame):
        pass

    for s in SENALES_MONITOREADAS:
        signal.signal(s, _noop)

    return r_fd


def leer_pendientes(r_fd):
    """Lee (sin bloquear) los numeros de senal que llegaron desde la
    ultima consulta. Puede devolver varias si llegaron varias juntas."""
    try:
        datos = os.read(r_fd, 1024)
        return list(datos)
    except BlockingIOError:
        return []
    except OSError:
        return []


def atender(numero_senal, contexto):
    """
    contexto es un dict con lo que el handler puede necesitar:
    stop_event, intervalos, config_path, snapshot, verbose.
    """
    stop_event = contexto["stop_event"]
    intervalos = contexto["intervalos"]
    snapshot = contexto["snapshot"]
    verbose = contexto["verbose"]
    config_path = contexto["config_path"]

    if numero_senal in (signal.SIGINT, signal.SIGTERM):
        stop_event.set()
        return "shutdown"

    if numero_senal == signal.SIGHUP:
        try:
            with open(config_path) as f:
                cfg = json.load(f)
            for vista, seg in cfg.get("intervals", {}).items():
                if vista in intervalos:
                    intervalos[vista].value = float(seg)
            return "config recargada"
        except Exception as e:
            return f"error recargando config: {e}"

    if numero_senal == signal.SIGUSR1:
        try:
            dump = {}
            for vista in ("resumen", "memoria", "fds", "threads",
                          "senales", "scheduling", "sistema"):
                try:
                    dump[vista] = dict(snapshot[vista])
                except Exception:
                    dump[vista] = {}
            nombre = f"dump_{int(time.time())}.json"
            with open(nombre, "w") as f:
                json.dump(dump, f, indent=2, default=str)
            return f"snapshot volcado a {nombre}"
        except Exception as e:
            return f"error en dump: {e}"

    if numero_senal == signal.SIGUSR2:
        verbose.value = 0 if verbose.value else 1
        return f"modo verbose {'activado' if verbose.value else 'desactivado'}"

    if numero_senal == signal.SIGWINCH:
        return "resize"

    return None
