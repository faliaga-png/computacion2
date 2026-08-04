# Monitor de Procesos y Threads (versión macOS)

**Computación II — Universidad de Mendoza — 2026**

Monitor de sistema en tiempo real, estilo `htop`, que muestra la anatomía
interna de cada proceso (memoria, threads, file descriptors, señales,
scheduling) leyendo APIs nativas de macOS (libproc vía `ctypes` + herramientas
del sistema vía `subprocess`), sin usar `psutil`.

---

## 1. Descripción general

El monitor es un sistema multiproceso: un recolector lista los PIDs
vivos, 7 analizadores (cada uno en su propio proceso) extraen una dimensión
distinta de información en paralelo y a su propio ritmo, y la escriben en
una estructura de memoria compartida (`multiprocessing.Manager().dict()`).
Un display en curses, corriendo en el proceso principal, lee esa memoria
compartida y la dibuja, sin bloquear a los analizadores.

### Cómo correr

```bash
pip install -r requirements.txt   # no hay dependencias externas, es un paso vacío
python3 src/main.py
# o bien:
make run
```

Requiere **macOS** (usa `ps`, `lsof`, `sysctl`, `vm_stat`, `vmmap`, que no
existen en Linux) y **Python 3.11+**.

### Uso

- La lista de procesos ocupa la parte de arriba de la pantalla; el panel de
  abajo muestra el detalle de la vista activa.
- `1`–`7` o `r/m/f/t/s/p/g` cambian de vista.
- `↑`/`↓` navegan la lista; `Enter` pinea el proceso seleccionado (así no se
  te mueve de la pantalla aunque cambie el orden).
- `/` filtra por comando, `u` filtra por usuario, `c` alterna el orden
  (CPU% / RSS / PID).
- `+`/`-` ajustan el intervalo de refresco de la vista activa (respetando un
  mínimo por vista, configurable en `config.json`).
- `h`/`?` muestra ayuda. `q` sale limpiamente.

### Señales (probalas desde otra terminal)

```bash
kill -HUP  <pid_del_monitor>   # recarga config.json (intervalos)
kill -USR1 <pid_del_monitor>   # vuelca el snapshot a dump_<timestamp>.json
kill -USR2 <pid_del_monitor>   # alterna modo verbose
kill -INT  <pid_del_monitor>   # shutdown limpio (o simplemente Ctrl+C)
```

---

## 2. Diagrama de arquitectura

```
                         ┌───────────────────────────────────┐
                         │        multiprocessing.Manager     │
                         │  (proceso servidor, vive aparte)   │
                         │                                     │
                         │  snapshot = {                      │
                         │    "resumen":    {data, ts},       │
                         │    "memoria":    {data, ts},       │
                         │    "fds":        {data, ts},       │
                         │    "threads":    {data, ts},       │
                         │    "senales":    {data, ts},       │
                         │    "scheduling": {data, ts},       │
                         │    "sistema":    {data, ts},       │
                         │    "_foco":      {pid},            │  <- lo escribe el display
                         │  }                                  │
                         │  pids_compartidos = [...]           │
                         └───────▲───────────────────▲────────┘
                    escriben (IPC)│                   │ leen (IPC)
       ┌────────────┬─────────────┼───────┬───────────┴─────────────┐
       │            │             │       │                          │
  ┌────▼───┐  ┌──────▼─────┐ ┌────▼───┐ …7 analizadores…      ┌──────▼──────┐
  │Recolec-│  │  resumen   │ │memoria │  fds / threads /      │  PROCESO    │
  │  tor   │  │  (2s)      │ │ (3s)   │  senales / scheduling /│  PRINCIPAL  │
  │ (1s)   │  └────────────┘ └────────┘  sistema (7 procesos) │  (display + │
  └────────┘                                                  │  señales)   │
                                                                └─────┬───────┘
                                                                      │
                                                          self-pipe (signal.set_wakeup_fd)
                                                                      │
                                                          SIGINT/TERM/HUP/USR1/USR2/WINCH
                                                          (llegan del SO al proceso principal)
```

8 procesos en total corriendo en paralelo: recolector + 7 analizadores. El
proceso principal (padre de todos) cumple el rol de **display** y de
**receptor de señales del monitor**; no es un analizador ni forma parte del
recolector.

---

## 3. Decisiones de diseño

### ¿Por qué `Manager().dict()` y no `Value`/`Array` para el snapshot?

`Value` y `Array` sirven para tipos de tamaño fijo y conocido de antemano
(un entero, un `float`, un array de N `double`). El snapshot que necesitamos
es un diccionario anidado y heterogéneo (listas de procesos con distinta
cantidad de campos, listas de threads que crecen y decrecen, strings de
longitud variable). Eso solo lo puede representar un objeto Python arbitrario,
y para compartir *objetos Python arbitrarios* entre procesos —no bytes de
tamaño fijo— hace falta un `Manager`: un proceso servidor aparte que guarda
el objeto real y expone un proxy a cada proceso cliente, serializando con
`pickle` en cada operación.

Sí usamos primitivas más livianas donde el dato es simple y de tamaño fijo:
- `multiprocessing.Value('d', ...)` para cada intervalo de refresco (un
  `float` por vista, 7 en total): no tiene sentido pagar el costo de un
  Manager para compartir un número.
- `multiprocessing.Value('i', ...)` para el flag `verbose` (on/off) y para
  `stop_event` usamos directamente `multiprocessing.Event()`, que por dentro
  es justamente un flag booleano con primitivas de sincronización ya
  resueltas por la librería.

### ¿Por qué cada analizador es un proceso y no un thread?

Parte del trabajo es CPU-bound (parsear texto de `ps`/`lsof`, decodificar
máscaras de señales) y otra parte es I/O-bound (esperar a que termine un
`subprocess.run`). Con threads, el GIL serializa igual la parte CPU-bound
—no hay paralelismo real ahí—, y además un cuelgue o excepción no controlada
en un thread puede dejar el proceso completo en un estado raro. Con procesos
separados: (a) hay paralelismo real para la parte CPU-bound, (b) si matás un
analizador con `kill -9`, el resto del monitor sigue funcionando (podés
probarlo vos mismo), y (c) cada analizador puede tener su propio intervalo
de refresco sin que nadie bloquee a nadie.

### Foco: por qué FDs/Threads/Memoria/Señales/Scheduling no barren TODOS los procesos

`ps_snapshot()` (usado por Resumen y Sistema) hace **una sola** invocación de
`ps` para traer datos de todos los procesos: barato. En cambio, `lsof`,
`ps -M` por proceso, `vmmap`, y `ps -o sig,sigmask` por proceso implican un
`fork+exec` por cada PID que quisiéramos inspeccionar. Pedirle eso para
cientos de procesos en cada tick haría que el monitor mismo consuma más CPU
que los procesos que está mirando. Por eso estos 5 analizadores solo piden
el detalle del proceso "en foco" (el seleccionado o pineado en la TUI), que
viaja de display → analizadores vía `snapshot["_foco"]["pid"]`.

### Race conditions: dónde podrían pasar y cómo se evitan

El punto más sensible es que **7 analizadores escriben concurrentemente** al
mismo `Manager().dict()`, y el display lee al mismo tiempo. Esto no genera
una race condition clásica de "lectura a mitad de escritura" porque cada
analizador escribe su **propia clave** (`snapshot["memoria"]`, cada uno la
suya) y nunca dos procesos escriben la misma clave a la vez. Además, cada
operación sobre un `Manager` proxy (`__setitem__`, `__getitem__`) es una
llamada RPC atómica al proceso servidor del Manager: el servidor procesa
un mensaje a la vez, así que no puede haber una escritura "a medio hacer"
visible desde otro proceso.

Donde sí *podría* haber una carrera es en `snapshot["_foco"]["pid"]`: el
display lo escribe, y los 5 analizadores de detalle lo leen. Pero es una
lectura de un solo valor (un PID, o `None`), no hay estado intermedio
inconsistente posible: en el peor caso, un analizador lee el foco "viejo"
un tick antes de que se actualice, y en el próximo tick ya tiene el nuevo.
Es una inconsistencia *temporal*, no una corrupción de datos.

### ¿Por qué esos intervalos por defecto?

Resumen/Threads/Sistema (2s) son las vistas que más cambian tick a tick y
las que primero mira cualquiera que abra el monitor. Memoria (3s) es un poco
más cara (agrega `vmmap` para el foco). FDs (5s) y sobre todo Señales/
Scheduling (10s) son vistas de datos que cambian poco de un segundo a otro,
así que no se justifica pagar el costo de refrescarlas seguido.

---

## 4. Conceptos del curso aplicados

- **Fork vs spawn** (clase 8): en Linux, `multiprocessing` usa `fork` por
  default (el hijo es una copia COW del padre, ya inicializado). En macOS,
  desde Python 3.8, el default es `spawn`: cada hijo arranca un intérprete
  nuevo y **reimporta `main.py` desde cero**. Por eso todo el código que crea
  procesos está protegido con `if __name__ == "__main__":` en `main.py` —
  si no lo estuviera, cada hijo, al reimportar el módulo, volvería a
  ejecutar el código que crea hijos, en una explosión recursiva de procesos.
  Lo comprobamos a propósito: al testear con un script sin ese guard, el
  `Manager()` se rompía con `EOFError` porque los hijos entraban en la misma
  recursión (ver `dudas.md`).

- **Copy-on-Write** (clase 4): es la razón por la que `fork` es barato en
  Linux (no copia memoria hasta que alguien escribe), y por la que en macOS
  con `spawn` *no* podemos depender de heredar estado ya inicializado del
  padre: hay que pasar todo explícitamente por argumentos al `Process()`.

- **`Manager` vs memoria compartida cruda** (clase 7 y 9): un `Manager` no
  es memoria compartida en el sentido de `mmap`; es un proceso servidor
  separado que otros procesos consultan por IPC (sockets/pipes internos).
  Es más lento que `Value`/`Array` (que sí usan `mmap` anónimo por debajo)
  pero mucho más flexible para estructuras de datos arbitrarias.

- **Señales y async-signal-safety** (clase 6): el patrón self-pipe
  (`manejador_senales.py`) evita que el signal handler real haga trabajo
  riesgoso. `signal.set_wakeup_fd()` hace que Python escriba automáticamente
  el número de señal a un pipe cuando llega; el loop principal lo lee de
  forma no bloqueante en un punto seguro de su propio ciclo (no dentro del
  handler), y ahí sí actúa (recargar config, volcar el snapshot, etc.).

- **Proceso vs thread** (clase 3/10): la arquitectura entera es multiproceso
  a propósito, para tener paralelismo real (sin GIL de por medio) y
  aislamiento de fallas entre analizadores.

- **PID/PPID, sesión y grupo de procesos**: se leen de `ps` (`pgid`, `sess`)
  en la vista Scheduling.

---

## 5. Limitaciones conocidas (comparadas con lo que Linux sí expone)

Ver la sección siguiente para el detalle comparado con la versión Linux del
TP. En resumen:

- **Handlers de señales instalados** (SigCgt): macOS no lo expone a otros
  procesos. Se documenta explícitamente en la vista Señales en vez de
  inventar un dato.
- **Señales pendientes al GRUPO** (equivalente a `ShdPnd` en Linux): no
  encontramos un campo de `ps` que lo exponga por separado de las
  pendientes del proceso; solo mostramos pendientes/bloqueadas a nivel
  proceso.
- **CPU affinity**: no accesible para procesos ajenos sin privilegios.
- **QoS class**: no hay forma simple de leerla desde afuera sin
  instrumentación privada (Instruments/dtrace con privilegios elevados).
- **Política de scheduling por thread y nombre del thread**: `ps -M` no
  expone esos dos campos (a diferencia de Mach `thread_info()` crudo, que sí
  los tendría pero requiere manejar `task_for_pid()` y sus restricciones de
  SIP). Mostramos estado y CPU% por thread, no política ni nombre.
- **Máscaras `sig`/`sigmask` de `ps`**: se decodifican bit a bit asumiendo el
  mismo layout que usa Linux para `SigBlk`/`SigPnd`. No verificamos con
  certeza absoluta que el kernel Darwin use exactamente el mismo mapeo de
  bits; lo tratamos como aproximación y lo aclaramos en el propio panel.
- **`vmmap`**: requiere que el usuario tenga permisos sobre el proceso
  inspeccionado (procesos de otro usuario o de `root` van a fallar
  silenciosamente y se muestra "sin permisos").
- El display y el manejo de señales del monitor comparten el **mismo
  proceso** (el principal), en vez de ser dos cajas separadas como sugiere
  el diagrama del enunciado — decisión de diseño explicada en la sección 3.

Lo que **sí se agregó** en una segunda pasada (no estaba en la primera
versión): GID (además de UID), args completos del proceso (`command` en
vez de `comm`), page faults minor/major vía `ps`, context switches
voluntarios/involuntarios (`nvcsw`/`nivcsw`), y el desglose de **CPU global
(user/system/idle %)**, calculado con delta entre dos lecturas de
`sysctl kern.cp_time` — este último faltaba por completo en la primera
versión y es un dato básico de la vista Sistema, así que valía la pena
priorizarlo.

---

## 6. Comparación con la versión Linux

- **Modelo de inspección**: Linux expone todo como archivos de texto bajo
  `/proc` (filosofía "todo es un archivo"); macOS no tiene `/proc` y expone
  la info vía llamadas a APIs (`libproc`, Mach) o vía herramientas CLI que
  internamente hacen esas llamadas. Esta versión usa sobre todo el segundo
  camino (`ps`, `lsof`, `vmmap`, `sysctl`, `vm_stat`) porque el enunciado lo
  habilita explícitamente y porque re-implementar en `ctypes` puro las
  estructuras `proc_taskallinfo`/`task_info` de macOS (que cambian entre
  versiones del SO y no están documentadas con la estabilidad de `/proc`)
  quedaba fuera de alcance razonable para el tiempo disponible.
- **Threads**: Linux expone cada thread como una entrada más en
  `/proc/<pid>/task/<tid>/`, prácticamente igual que un proceso. macOS
  requiere `task_for_pid()` (con restricciones de SIP/entitlements para
  procesos ajenos) para llegar a los Mach threads vía syscalls; acá se opta
  por `ps -M`, que internamamente ya resuelve eso sin pedirnos privilegios
  especiales, a costa de exponer menos detalle por thread que Mach crudo.
- **Señales**: Linux muestra bloqueadas/ignoradas/con-handler/pendientes
  como 4 máscaras separadas en `/proc/<pid>/status`. macOS, vía `ps`, solo
  nos da pendientes y bloqueadas; los handlers instalados simplemente no
  están expuestos a terceros procesos en macOS — es una limitación real del
  sistema operativo, no de nuestra implementación.
- **Scheduling**: Linux tiene `nice`/`priority`/política POSIX
  (OTHER/FIFO/RR) expuestos por proceso. macOS superpone un sistema más
  moderno de **QoS classes** (USER_INTERACTIVE, USER_INITIATED, UTILITY,
  BACKGROUND) donde el kernel decide la prioridad real en base a la
  "intención" declarada por la app, no solo un número de nice — pero leerlo
  desde afuera de un proceso ajeno no es directo, así que en esta versión
  solo mostramos `nice` clásico (vía `os.getpriority`, que sí es portable).
- Lo que más nos costó: entender que **muchas APIs "equivalentes" a
  `/proc` en macOS requieren privilegios que un proceso normal no tiene**
  para inspeccionar procesos ajenos (no solo de otro usuario: incluso
  procesos del sistema del mismo usuario a veces). Eso llevó a apoyarnos en
  herramientas CLI (que corren con sus propios privilegios/entitlements ya
  resueltos por Apple) en vez de replicar todo a mano con `ctypes`.

---

## 7. Lo que aprendí

  Este TP me encanto y me ayudo mucho para comprender mejor todos los temas, para poder practicarlos en un proyecto en conjunto. Si bien hay bastantes cosas que no sabia como hacer o como seguir, fui usando Claude para que me explique y guie para poder hacerlo lo mejor posible, aprendiendo o ampliando lo que ya sabia en cada consulta.

---

## 8. Estructura del repo

```
.
├── README.md
├── dudas.md
├── Makefile
├── requirements.txt
├── config.json
├── src/
│   ├── main.py                  # entry point, arma Manager + procesos + señales
│   ├── recolector.py            # lista PIDs vivos cada 1s
│   ├── macos_api.py             # TODA la interacción con el SO (ctypes + subprocess)
│   ├── manejador_senales.py     # self-pipe para señales del monitor mismo
│   ├── display.py               # TUI en curses, las 7 vistas
│   └── analizadores/
│       ├── resumen.py
│       ├── memoria.py
│       ├── fds.py
│       ├── threads.py
│       ├── senales.py           # señales de los procesos observados (no del monitor)
│       ├── scheduling.py
│       └── sistema.py
└── tests/
    └── smoke_test.py            # levanta todo el sistema sin la TUI y valida
                                  # que los 7 analizadores escriban datos sin error
```
