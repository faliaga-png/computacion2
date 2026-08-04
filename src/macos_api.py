"""
macos_api.py
------------
Capa de acceso al sistema operativo. Concentra TODO el código que habla
con macOS, para que el resto del programa (analizadores, display) no
sepa nada de ctypes ni de parseo de `ps`/`lsof`/`sysctl`.

Dos caminos, como permite el enunciado:
  1. ctypes sobre libproc, para el listado de PIDs (proc_listpids).
  2. subprocess sobre herramientas nativas (ps, lsof, vm_stat, sysctl)
     para el resto: es la vía "engorrosa de evitar con ctypes puro"
     que el enunciado habilita explícitamente.

No se usa psutil ni ninguna librería que envuelva esto por nosotros.
"""
import ctypes
import ctypes.util
import subprocess
import signal
import os
import time
import pwd

# ---------------------------------------------------------------------------
# 1) Listado de PIDs vía libproc (ctypes)
# ---------------------------------------------------------------------------

_libc = ctypes.CDLL(ctypes.util.find_library("c"))
PROC_ALL_PIDS = 1


def list_pids():
    """
    Lista todos los PIDs activos del sistema usando proc_listpids().
    Patrón típico de esta API: se llama una vez para saber cuántos
    bytes hacen falta, y una segunda vez para llenar el buffer.
    """
    _libc.proc_listpids.restype = ctypes.c_int
    n = _libc.proc_listpids(PROC_ALL_PIDS, 0, None, 0)
    if n <= 0:
        return []
    count = n // ctypes.sizeof(ctypes.c_int)
    buf = (ctypes.c_int * count)()
    _libc.proc_listpids(PROC_ALL_PIDS, 0, buf, n)
    return sorted({p for p in buf if p != 0})


# ---------------------------------------------------------------------------
# Helper genérico para invocar binarios del sistema
# ---------------------------------------------------------------------------

def _run(cmd, timeout=3):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.stdout
    except Exception:
        return ""


def _is_float(s):
    try:
        float(s)
        return True
    except (ValueError, TypeError):
        return False


def _is_int(s):
    try:
        int(s)
        return True
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# 2) Datos básicos de todos los procesos (vista Resumen / Memoria)
# ---------------------------------------------------------------------------

def ps_snapshot():
    """
    UNA sola invocación de `ps` trae los datos básicos de TODOS los
    procesos de una vez. A propósito NO llamamos a ps una vez por PID:
    eso implicaría un fork+exec por proceso inspeccionado, carísimo y
    no escalaría con cientos de procesos corriendo.

    Usamos `command` (no `comm`) como último campo para traer los ARGS
    COMPLETOS del proceso, no solo el nombre corto del ejecutable —
    equivalente a leer /proc/<pid>/cmdline en Linux. Como es el último
    campo, `split(None, N)` lo captura entero (con espacios y todo) sin
    que rompa el parseo de las columnas anteriores.

    También sumamos `gid` (grupo, para completar UID/GID que pide la
    consigna) y `majflt`/`minflt` (page faults). OJO: estos dos últimos
    no están tan documentados para macOS como para Linux; si tu ps no
    los soporta, van a venir como "-" y los mostramos igual sin inventar
    un número.
    """
    campos = "pid,ppid,uid,gid,state,pcpu,rss,vsz,nlwp,pgid,sess,nice,pri,majflt,minflt"
    out = _run(["ps", "-axo", campos + ",command"])
    procesos = {}
    for linea in out.splitlines()[1:]:
        partes = linea.split(None, 15)
        if len(partes) < 16:
            continue
        (pid_s, ppid_s, uid_s, gid_s, estado, pcpu, rss, vsz, nlwp, pgid, sess,
         nice, pri, majflt, minflt, comando) = partes
        if not _is_int(pid_s):
            continue
        pid = int(pid_s)
        try:
            usuario = pwd.getpwuid(int(uid_s)).pw_name
        except (KeyError, ValueError):
            usuario = uid_s
        procesos[pid] = {
            "pid": pid,
            "ppid": int(ppid_s) if _is_int(ppid_s) else -1,
            "uid": uid_s,
            "gid": gid_s,
            "usuario": usuario,
            "estado": estado,
            "cpu_pct": float(pcpu) if _is_float(pcpu) else 0.0,
            "rss_kb": int(rss) if _is_int(rss) else 0,
            "vsz_kb": int(vsz) if _is_int(vsz) else 0,
            "nthreads": int(nlwp) if _is_int(nlwp) else 0,
            "pgid": pgid,
            "sid": sess,
            "nice": nice,
            "pri": pri,
            "majflt": majflt,
            "minflt": minflt,
            "comando": comando,
        }
    return procesos


def cmdline_completo(pid):
    """Args completos del proceso (equivalente a /proc/<pid>/cmdline)."""
    out = _run(["ps", "-o", "command=", "-p", str(pid)])
    return out.strip()


# ---------------------------------------------------------------------------
# 3) Memoria: regiones vía vmmap (opcional, caro) + resumen de faults
# ---------------------------------------------------------------------------

def memoria_detalle(pid):
    """
    Además de RSS/VSZ (que ya trae ps_snapshot), agrupamos las regiones
    de memoria vía `vmmap -summary`, que es el equivalente aproximado a
    /proc/<pid>/maps agrupado. vmmap es costoso, por eso esta función se
    llama con el intervalo más largo (vista Memoria, 3s+) y no en cada
    tick del recolector.
    """
    out = _run(["vmmap", "-summary", str(pid)], timeout=5)
    if not out:
        return {"regiones": [], "resumen_raw": "vmmap no disponible o sin permisos"}
    regiones = []
    for linea in out.splitlines():
        low = linea.strip()
        if low.startswith(("__TEXT", "__DATA", "__LINKEDIT", "Stack", "Heap",
                            "MALLOC", "Kernel Alloc", "mapped file", "shared memory")):
            regiones.append(low)
    return {"regiones": regiones[:25], "resumen_raw": "\n".join(out.splitlines()[:5])}


# ---------------------------------------------------------------------------
# 4) File descriptors vía lsof
# ---------------------------------------------------------------------------

def fds_de(pid, limite=64):
    """
    Lista FDs abiertos vía `lsof -Ffftn` (salida en formato de campos,
    fácil de parsear sin ambigüedad de columnas). f=fd, t=tipo, n=destino.
    """
    out = _run(["lsof", "-p", str(pid), "-Ffftn"], timeout=5)
    fds = []
    actual = {}
    for linea in out.splitlines():
        if not linea:
            continue
        tag, val = linea[0], linea[1:]
        if tag == "f":
            if actual:
                fds.append(actual)
            actual = {"fd": val}
        elif tag == "t":
            actual["tipo"] = val
        elif tag == "n":
            actual["destino"] = val
    if actual:
        fds.append(actual)
    return fds[:limite]


# ---------------------------------------------------------------------------
# 5) Threads (Mach threads / LWPs) vía `ps -M`
# ---------------------------------------------------------------------------

def threads_de(pid):
    """
    `ps -M` es el flag de macOS que expone los threads (LWPs) de un
    proceso, análogo a listar /proc/<pid>/task/ en Linux, pero sin
    tener que llamar a task_for_pid()/task_threads() (que en macOS
    moderno requiere entitlements especiales para procesos ajenos).
    """
    out = _run(["ps", "-M", "-p", str(pid), "-o", "lwp,state,time,pcpu"])
    threads = []
    for linea in out.splitlines()[1:]:
        partes = linea.split()
        if len(partes) < 4:
            continue
        lwp, estado, tcpu, pcpu = partes[0], partes[1], partes[2], partes[3]
        threads.append({
            "tid": lwp,
            "estado": estado,
            "tiempo_cpu": tcpu,
            "cpu_pct": float(pcpu) if _is_float(pcpu) else 0.0,
        })
    return threads


# ---------------------------------------------------------------------------
# 6) Señales
# ---------------------------------------------------------------------------

_NOMBRES_SENAL = {s.value: s.name for s in signal.Signals}


def _decodificar_mascara(hexstr):
    """Convierte una máscara hex de señales en nombres legibles (SIGTERM, ...)."""
    try:
        mask = int(hexstr, 16)
    except (ValueError, TypeError):
        return []
    nombres = []
    for bit, nombre in _NOMBRES_SENAL.items():
        if bit > 0 and (mask >> (bit - 1)) & 1:
            nombres.append(nombre)
    return sorted(nombres)


def senales_de(pid):
    """
    macOS expone señales pendientes/bloqueadas vía las keywords `sig` y
    `sigmask` de ps. NO expone los handlers instalados (SigCgt en Linux)
    a otros procesos: es información privada del proceso que instaló el
    handler. Esta es una limitación real de macOS frente a Linux, y la
    documentamos en vez de inventar un dato que el SO no da.
    """
    out = _run(["ps", "-o", "sig,sigmask", "-p", str(pid)])
    lineas = out.splitlines()
    if len(lineas) < 2:
        return {"pendientes": [], "bloqueadas": [],
                "handler": "proceso no encontrado"}
    partes = lineas[1].split()
    pend = partes[0] if len(partes) > 0 else ""
    bloq = partes[1] if len(partes) > 1 else ""
    return {
        "pendientes": _decodificar_mascara(pend),
        "bloqueadas": _decodificar_mascara(bloq),
        "handler": "no expuesto por macOS a procesos externos (limitación del SO)",
    }


# ---------------------------------------------------------------------------
# 7) Scheduling
# ---------------------------------------------------------------------------

def scheduling_de(pid):
    """
    `os.getpriority` es stdlib de Python y llama directo a getpriority(2),
    no hace falta ctypes para esto. El resto sale de `ps`.
    QoS class y CPU affinity NO están disponibles para procesos ajenos
    sin privilegios especiales: se documentan como limitación.
    """
    try:
        nice = os.getpriority(os.PRIO_PROCESS, pid)
    except (ProcessLookupError, PermissionError):
        nice = None
    out = _run(["ps", "-o", "pri,pgid,sess,time,utime,nvcsw,nivcsw", "-p", str(pid)])
    lineas = out.splitlines()
    pri = pgid = sid = tiempo = utime = nvcsw = nivcsw = None
    if len(lineas) >= 2:
        partes = lineas[1].split()
        if len(partes) >= 7:
            pri, pgid, sid, tiempo, utime, nvcsw, nivcsw = partes[:7]
    return {
        "nice": nice,
        "priority": pri,
        "pgid": pgid,
        "sid": sid,
        "tiempo_cpu_total": tiempo,
        # context switches: nvcsw = voluntarios (el proceso cedió la CPU,
        # ej. esperando I/O), nivcsw = involuntarios (el scheduler se la
        # quitó, ej. se le acabó el quantum). Ver clase de Scheduler.
        "ctx_switches_voluntarios": nvcsw,
        "ctx_switches_involuntarios": nivcsw,
        "qos": "no disponible sin instrumentación privada (limitación de macOS)",
        "affinity": "no expuesta a procesos sin privilegios en macOS (limitación)",
    }


# ---------------------------------------------------------------------------
# 8) Stats globales del sistema
# ---------------------------------------------------------------------------

def _parse_vm_stat(texto):
    stats = {}
    for linea in texto.splitlines():
        if ":" not in linea:
            continue
        clave, valor = linea.split(":", 1)
        valor = valor.strip().rstrip(".")
        if valor.isdigit():
            stats[clave.strip()] = int(valor)
    return stats


def _uptime_desde_boottime(boottime_raw):
    # formato típico: "{ sec = 1234567890, usec = 0 } Tue Jan  1 00:00:00 2026"
    try:
        sec = int(boottime_raw.split("sec = ")[1].split(",")[0])
        return max(0.0, time.time() - sec)
    except Exception:
        return 0.0


def cpu_global_ticks():
    """
    `sysctl -n kern.cp_time` devuelve 4 contadores ACUMULATIVOS de ticks
    de reloj desde el boot, agregados de todos los cores: user, nice,
    system, idle (en ese orden, como en BSD). Son contadores crecientes,
    no un porcentaje: para sacar %CPU hay que leerlos dos veces con un
    intervalo y calcular el delta, exactamente igual que se hace con los
    campos utime/stime de /proc/<pid>/stat en Linux.
    """
    out = _run(["sysctl", "-n", "kern.cp_time"]).strip()
    partes = out.split()
    if len(partes) < 4:
        return None
    try:
        user, nice, system, idle = (int(x) for x in partes[:4])
        return {"user": user, "nice": nice, "system": system, "idle": idle}
    except ValueError:
        return None


def sistema_snapshot():
    ncpu = _run(["sysctl", "-n", "hw.ncpu"]).strip()
    memsize = _run(["sysctl", "-n", "hw.memsize"]).strip()
    loadavg = _run(["sysctl", "-n", "vm.loadavg"]).strip()
    swap = _run(["sysctl", "-n", "vm.swapusage"]).strip()
    boottime_raw = _run(["sysctl", "-n", "kern.boottime"]).strip()
    vmstat_raw = _run(["vm_stat"])
    vmstat = _parse_vm_stat(vmstat_raw)

    return {
        "ncpu": int(ncpu) if _is_int(ncpu) else 0,
        "mem_total_bytes": int(memsize) if _is_int(memsize) else 0,
        "loadavg_raw": loadavg,
        "swap_raw": swap,
        "vm_stat": vmstat,
        "uptime_seg": _uptime_desde_boottime(boottime_raw),
        "boottime_raw": boottime_raw,
    }
