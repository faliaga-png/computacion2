"""
display.py
-----------
Corre en el proceso PRINCIPAL (no es un proceso analizador mas). Se
encarga de: dibujar la TUI con curses, leer el teclado, y atender el
pipe de senales (ver manejador_senales.py) en el mismo loop.

Decision de diseno: podriamos haber puesto el display en un proceso
aparte (como sugiere el diagrama del enunciado), pero eso complica
mucho la sincronizacion entre "el proceso que recibe la senal real
del SO" (que siempre es el proceso principal) y "el proceso que dibuja
la pantalla". Al fusionarlos, el mismo loop que lee teclado tambien
puede atender senales sin IPC extra. Es una simplificacion consciente,
documentada aca y en el README.
"""
import curses
import os
import time

VISTAS = ["resumen", "memoria", "fds", "threads", "senales", "scheduling", "sistema"]
TITULOS = {
    "resumen": "RESUMEN", "memoria": "MEMORIA", "fds": "FILE DESCRIPTORS",
    "threads": "THREADS", "senales": "SENALES", "scheduling": "SCHEDULING",
    "sistema": "SISTEMA GLOBAL",
}
TECLAS_VISTA = {
    ord('1'): 0, ord('r'): 0,
    ord('2'): 1, ord('m'): 1,
    ord('3'): 2, ord('f'): 2,
    ord('4'): 3, ord('t'): 3,
    ord('5'): 4, ord('s'): 4,
    ord('6'): 5, ord('p'): 5,
    ord('7'): 6, ord('g'): 6,
}


def correr(stdscr, snapshot, intervalos, pids_compartidos, stop_event, verbose,
           config_path, r_fd_senales, min_intervals):
    import manejador_senales

    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(200)

    vista_idx = 0
    sel = 0
    pinned = None
    filtro_cmd = ""
    filtro_user = ""
    sort_mode = "cpu"  # cpu | rss | pid
    help_on = False
    mensaje = ""
    mensaje_hasta = 0

    contexto_senales = {
        "stop_event": stop_event, "intervalos": intervalos,
        "snapshot": snapshot, "verbose": verbose, "config_path": config_path,
    }

    while not stop_event.is_set():
        # 1) atender senales pendientes (self-pipe)
        for num in manejador_senales.leer_pendientes(r_fd_senales):
            resultado = manejador_senales.atender(num, contexto_senales)
            if resultado == "shutdown":
                break
            if resultado:
                mensaje = f"[senal] {resultado}"
                mensaje_hasta = time.time() + 3
            if resultado == "resize":
                curses.update_lines_cols()

        if stop_event.is_set():
            break

        # 2) leer teclado
        try:
            ch = stdscr.getch()
        except curses.error:
            ch = -1

        if ch != -1:
            if ch == ord('q'):
                stop_event.set()
                break
            elif ch in TECLAS_VISTA:
                vista_idx = TECLAS_VISTA[ch]
            elif ch == curses.KEY_UP:
                sel = max(0, sel - 1)
            elif ch == curses.KEY_DOWN:
                sel += 1
            elif ch in (10, 13, curses.KEY_ENTER):
                pid_actual = _pid_seleccionado(snapshot, sel, filtro_cmd, filtro_user, sort_mode)
                pinned = None if pinned == pid_actual else pid_actual
            elif ch == ord('/'):
                filtro_cmd = _pedir_texto(stdscr, "Filtrar por comando: ")
            elif ch == ord('u'):
                filtro_user = _pedir_texto(stdscr, "Filtrar por usuario: ")
            elif ch == ord('c'):
                sort_mode = {"cpu": "rss", "rss": "pid", "pid": "cpu"}[sort_mode]
            elif ch in (ord('+'), ord('=')):
                _ajustar_intervalo(intervalos, VISTAS[vista_idx], +0.5, min_intervals)
            elif ch == ord('-'):
                _ajustar_intervalo(intervalos, VISTAS[vista_idx], -0.5, min_intervals)
            elif ch in (ord('h'), ord('?')):
                help_on = not help_on

        # 3) actualizar el "foco" que consumen los analizadores de detalle
        foco = pinned if pinned is not None else _pid_seleccionado(
            snapshot, sel, filtro_cmd, filtro_user, sort_mode)
        try:
            snapshot["_foco"]["pid"] = foco
        except Exception:
            pass

        # 4) dibujar
        if time.time() > mensaje_hasta:
            mensaje = ""
        try:
            _dibujar(stdscr, snapshot, vista_idx, sel, pinned, filtro_cmd,
                     filtro_user, sort_mode, help_on, intervalos, verbose, mensaje)
        except curses.error:
            pass  # terminal muy chica en este tick puntual; se recupera solo


def _lista_procesos(snapshot, filtro_cmd, filtro_user, sort_mode):
    try:
        procesos = dict(snapshot["resumen"]["data"])
    except Exception:
        return []
    items = list(procesos.values())
    if filtro_cmd:
        items = [p for p in items if filtro_cmd.lower() in p.get("comando", "").lower()]
    if filtro_user:
        items = [p for p in items if filtro_user.lower() in p.get("usuario", "").lower()]
    claves = {"cpu": lambda p: -p.get("cpu_pct", 0), "rss": lambda p: -p.get("rss_kb", 0),
              "pid": lambda p: p.get("pid", 0)}
    items.sort(key=claves[sort_mode])
    return items


def _pid_seleccionado(snapshot, sel, filtro_cmd, filtro_user, sort_mode):
    items = _lista_procesos(snapshot, filtro_cmd, filtro_user, sort_mode)
    if not items:
        return None
    sel = min(sel, len(items) - 1)
    return items[sel]["pid"]


def _ajustar_intervalo(intervalos, vista, delta, min_intervals):
    val = intervalos[vista]
    minimo = min_intervals.get(vista, 0.5)
    val.value = max(minimo, round(val.value + delta, 2))


def _pedir_texto(stdscr, prompt):
    curses.curs_set(1)
    stdscr.nodelay(False)
    alto, ancho = stdscr.getmaxyx()
    stdscr.move(alto - 1, 0)
    stdscr.clrtoeol()
    stdscr.addstr(alto - 1, 0, prompt)
    curses.echo()
    try:
        texto = stdscr.getstr(alto - 1, len(prompt), 40).decode("utf-8", "ignore")
    except Exception:
        texto = ""
    curses.noecho()
    curses.curs_set(0)
    stdscr.nodelay(True)
    return texto.strip()


def _dibujar(stdscr, snapshot, vista_idx, sel, pinned, filtro_cmd, filtro_user,
             sort_mode, help_on, intervalos, verbose, mensaje):
    stdscr.erase()
    alto, ancho = stdscr.getmaxyx()
    vista = VISTAS[vista_idx]

    encabezado = (f" Monitor de Procesos (macOS) | vista: {TITULOS[vista]} "
                  f"| orden: {sort_mode} | intervalo: {intervalos[vista].value:.1f}s "
                  f"| verbose: {'on' if verbose.value else 'off'} ")
    stdscr.addnstr(0, 0, encabezado.ljust(ancho), ancho, curses.A_REVERSE)

    if help_on:
        _dibujar_ayuda(stdscr, alto, ancho)
        return

    items = _lista_procesos(snapshot, filtro_cmd, filtro_user, sort_mode)
    filtros_txt = f" filtros: cmd='{filtro_cmd}' user='{filtro_user}' | pin={pinned}"
    stdscr.addnstr(1, 0, filtros_txt.ljust(ancho), ancho, curses.A_DIM)

    cab = f"{'PID':>7} {'PPID':>7} {'USR':<10} {'ST':<3} {'CPU%':>6} {'RSS(KB)':>10} {'THR':>4} COMANDO"
    stdscr.addnstr(2, 0, cab.ljust(ancho), ancho, curses.A_BOLD)

    filas_lista = max(3, (alto * 2) // 5)
    for i, p in enumerate(items[:filas_lista]):
        fila = 3 + i
        if fila >= alto - 1:
            break
        marca = ">" if i == min(sel, len(items) - 1) else " "
        pin_marca = "*" if p["pid"] == pinned else " "
        linea = (f"{marca}{p['pid']:>6}{pin_marca}{p['ppid']:>7} {p['usuario'][:10]:<10} "
                 f"{p['estado']:<3} {p['cpu_pct']:>6.1f} {p['rss_kb']:>10} "
                 f"{p['nthreads']:>4} {p['comando'][:ancho]}")
        attr = curses.A_REVERSE if i == min(sel, len(items) - 1) else curses.A_NORMAL
        stdscr.addnstr(fila, 0, linea, ancho, attr)

    linea_sep = 3 + filas_lista + 1
    if linea_sep < alto - 1:
        stdscr.addnstr(linea_sep, 0, "-" * ancho, ancho)
        _dibujar_detalle(stdscr, snapshot, vista, linea_sep + 1, alto, ancho)

    pie = mensaje if mensaje else "1-7/rmftspg vista | ↑↓ nav | Enter pin | / filtrar cmd | u user | c orden | +/- intervalo | h ayuda | q salir"
    stdscr.addnstr(alto - 1, 0, pie.ljust(ancho), ancho, curses.A_DIM)
    stdscr.refresh()


def _dibujar_detalle(stdscr, snapshot, vista, y0, alto, ancho):
    try:
        data = dict(snapshot[vista])
        payload = data.get("data", {})
        ts = data.get("ts", 0)
    except Exception:
        payload, ts = {}, 0

    edad = f"(hace {time.time() - ts:.1f}s)" if ts else "(sin datos aun)"
    y = y0
    stdscr.addnstr(y, 0, f"Detalle [{TITULOS[vista]}] {edad}", ancho, curses.A_BOLD)
    y += 1

    lineas = _formatear_detalle(vista, payload)
    for linea in lineas:
        if y >= alto - 2:
            break
        stdscr.addnstr(y, 0, linea, ancho)
        y += 1


def _formatear_detalle(vista, payload):
    lineas = []
    if vista == "memoria":
        foco = payload.get("pid_foco")
        lineas.append(f"PID en foco: {foco}")
        det = payload.get("detalle_foco", {})
        for r in det.get("regiones", [])[:10]:
            lineas.append(f"  {r}")
        if not det.get("regiones"):
            lineas.append("  (seleccioná un proceso con ↑↓ para ver sus regiones)")
    elif vista == "fds":
        foco = payload.get("pid_foco")
        lineas.append(f"PID en foco: {foco}  (mostrando hasta el límite configurado)")
        for fd in payload.get("fds", [])[:15]:
            lineas.append(f"  fd={fd.get('fd','?'):<4} tipo={fd.get('tipo','?'):<6} {fd.get('destino','')}")
    elif vista == "threads":
        foco = payload.get("pid_foco")
        lineas.append(f"PID en foco: {foco}")
        for t in payload.get("threads", [])[:15]:
            lineas.append(f"  tid={t['tid']:<8} estado={t['estado']:<4} cpu%={t['cpu_pct']:>5.1f} tiempo={t['tiempo_cpu']}")
    elif vista == "senales":
        foco = payload.get("pid_foco")
        lineas.append(f"PID en foco: {foco}")
        lineas.append(f"  Pendientes: {', '.join(payload.get('pendientes', [])) or '(ninguna)'}")
        lineas.append(f"  Bloqueadas: {', '.join(payload.get('bloqueadas', [])) or '(ninguna)'}")
        lineas.append(f"  Handlers:   {payload.get('handler', '')}")
    elif vista == "scheduling":
        foco = payload.get("pid_foco")
        lineas.append(f"PID en foco: {foco}")
        for clave in ("nice", "priority", "pgid", "sid", "tiempo_cpu_total",
                      "ctx_switches_voluntarios", "ctx_switches_involuntarios",
                      "qos", "affinity"):
            if clave in payload:
                lineas.append(f"  {clave}: {payload[clave]}")
    elif vista == "sistema":
        cpu = payload.get("cpu_global_pct", {})
        lineas.append(f"  CPU global: user={cpu.get('user',0):.1f}%  "
                       f"system={cpu.get('system',0):.1f}%  idle={cpu.get('idle',0):.1f}%")
        for clave in ("ncpu", "mem_total_bytes", "uptime_seg", "total_procesos", "total_threads"):
            if clave in payload:
                lineas.append(f"  {clave}: {payload[clave]}")
        lineas.append(f"  por_estado: {payload.get('por_estado', {})}")
        lineas.append(f"  loadavg: {payload.get('loadavg_raw','')}")
        lineas.append(f"  swap: {payload.get('swap_raw','')}")
        lineas.append("  top CPU: " + ", ".join(
            f"{p['pid']}:{p['comando'][:12]}({p['cpu_pct']:.1f}%)" for p in payload.get("top_cpu", [])))
        lineas.append("  top MEM: " + ", ".join(
            f"{p['pid']}:{p['comando'][:12]}({p['rss_kb']}KB)" for p in payload.get("top_mem", [])))
    elif vista == "resumen":
        lineas.append("  (ver la lista de arriba; este panel se usa para detalle en otras vistas)")
    return lineas


def _dibujar_ayuda(stdscr, alto, ancho):
    ayuda = [
        "AYUDA - Monitor de Procesos (macOS)",
        "",
        "1-7 o r/m/f/t/s/p/g   cambiar de vista",
        "↑ / ↓                 navegar la lista de procesos",
        "Enter                 pinear/despinear el proceso seleccionado",
        "/                     filtrar por nombre de comando",
        "u                     filtrar por usuario",
        "c                     alternar orden (CPU% / RSS / PID)",
        "+ / -                 ajustar el intervalo de refresco de la vista activa",
        "h / ?                 esta ayuda",
        "q                     salir limpiamente",
        "",
        "Señales al proceso (desde otra terminal, con `kill -SIG <pid>`):",
        "  SIGHUP   recargar config.json",
        "  SIGUSR1  volcar snapshot a dump_<ts>.json",
        "  SIGUSR2  alternar modo verbose",
        "",
        "Presioná h de nuevo para volver.",
    ]
    for i, linea in enumerate(ayuda):
        if i + 2 >= alto:
            break
        stdscr.addnstr(i + 2, 2, linea, ancho - 2)
    stdscr.refresh()
