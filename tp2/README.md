# TP2 — Plataforma de Tareas Distribuidas

Computación II — Universidad de Mendoza — 2026

Un servicio HTTP al que se le encargan trabajos. Los ejecuta de forma
concurrente con un pool de workers (corrutinas de `asyncio`) y permite
consultar su estado y resultado.

---

## 1. Descripción y uso

### Levantar

```bash
cd tp2
docker compose up --build
```

La API queda en `http://localhost:8000` (documentación automática en
`http://localhost:8000/docs`).

Sin Docker (Python 3.11+):

```bash
cd tp2
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python src/main.py
```

### Configuración (variables de entorno, en `docker-compose.yml`)

| Variable | Por defecto | Qué hace |
|----------|-------------|----------|
| `CANT_WORKERS` | 4 | Cantidad de corrutinas worker |
| `TIMEOUT_POR_DEFECTO` | 60 | Timeout en segundos si la tarea no trae uno |
| `USAR_EXECUTOR` | true | `false` corre `hashear` en el event loop (sólo para la medición 2) |
| `TAMANO_COLA` | 1000 | Máximo de tareas pendientes; si se llena, el POST da 503 |
| `LIMITE_APAGADO` | 10 | Segundos que el apagado espera a las tareas en curso |
| `DIRECTORIO_VOLCADOS` | `volcados` | Dónde se guarda `estado_<timestamp>.json` |

### Endpoints

| Método | Ruta | Qué hace |
|--------|------|----------|
| `POST` | `/tareas` | Encola una tarea. Responde enseguida con `id` y estado (`202`) |
| `GET` | `/tareas?estado=...` | Lista las tareas, con filtro opcional por estado |
| `GET` | `/tareas/{id}` | Estado y resultado de una tarea |
| `DELETE` | `/tareas/{id}` | Cancela una tarea pendiente o en ejecución |
| `GET` | `/estadisticas` | Totales por estado, tiempos promedio, workers ocupados |

Estados: `pendiente`, `ejecutando`, `completada`, `fallida`, `cancelada`.

### Ejemplos con curl

```bash
# descargar: varias URLs en paralelo
curl -X POST localhost:8000/tareas -H 'Content-Type: application/json' \
  -d '{"tipo": "descargar", "parametros": {"urls": ["https://www.python.org", "https://www.um.edu.ar"]}}'
# {"id":"d96c946e","estado":"pendiente"}

# hashear: SHA-256 aplicado N veces (CPU-bound)
curl -X POST localhost:8000/tareas -H 'Content-Type: application/json' \
  -d '{"tipo": "hashear", "parametros": {"texto": "hola", "iteraciones": 5000000}}'

# esperar: duerme N segundos, con un timeout propio de 5 s
curl -X POST localhost:8000/tareas -H 'Content-Type: application/json' \
  -d '{"tipo": "esperar", "parametros": {"segundos": 30}, "timeout": 5}'

# consultar
curl localhost:8000/tareas/d96c946e
curl "localhost:8000/tareas?estado=completada"
curl localhost:8000/estadisticas

# cancelar (funciona aunque ya esté ejecutando)
curl -X DELETE localhost:8000/tareas/d96c946e

# apagado ordenado (manda SIGTERM)
docker compose stop
```

Respuestas de error: `400` por parámetros inválidos, `404` si el id no existe,
`409` si se cancela una tarea que ya terminó y `503` si la cola está llena o el
servidor se está apagando.

`descargar` acepta además `"concurrente": false` para bajar las URLs una por
una. Existe sólo para la medición 1.

---

## 2. Arquitectura

```
                         proceso Python (un solo hilo con el event loop)
  ┌────────┐   HTTP   ┌──────────────────────────────────────────────────────────┐
  │cliente │ ───────> │  uvicorn + FastAPI (main.py)                             │
  └────────┘ <─────── │   POST /tareas ──> valida ──> plataforma.encolar()       │
      ^      202 + id │                                  │                       │
      │               │                                  v                       │
      │               │     self.tareas {id: dict}   asyncio.Queue (ids)         │
      │  GET /tareas/id   (estado compartido)            │                       │
      └───────────────│──────── lee ───────┐             │ await cola.get()      │
                      │                    │             v                       │
                      │               ┌─────────────────────────────┐            │
                      │               │ worker 0 .. worker N-1      │            │
                      │               │ (corrutinas, plataforma.py) │            │
                      │               └─────────────┬───────────────┘            │
                      │                             │ create_task(wait_for(...)) │
                      │         ┌───────────────────┼──────────────────┐         │
                      │         v                   v                  v         │
                      │    descargar           esperar             hashear       │
                      │  httpx async +      asyncio.sleep     run_in_executor ───│──┐
                      │  asyncio.gather                                          │  │
                      └──────────────────────────────────────────────────────────┘  │
                                                                                     v
                                                         ┌───────────────────────────────┐
                                                         │ ProcessPoolExecutor           │
                                                         │ (un proceso por núcleo)       │
                                                         │ tareas.hashear() en paralelo  │
                                                         └───────────────────────────────┘
```

### Flujo de una tarea

1. `POST /tareas`: se valida, se crea el diccionario con estado `pendiente`
   y se hace `cola.put_nowait(id)`. Se responde **sin esperar** a la tarea.
2. Un worker libre que estaba suspendido en `await cola.get()` recibe el id.
   Si la tarea fue cancelada mientras esperaba, la saltea.
3. El worker la marca `ejecutando` y la corre en su propia `asyncio.Task`,
   envuelta en `asyncio.wait_for(..., timeout)`.
4. Según cómo termine queda `completada` (con `resultado`), `fallida`
   (con `error`: timeout o excepción) o `cancelada`.
5. El worker llama a `cola.task_done()` y vuelve a esperar en la cola.

### Archivos

| Archivo | Contenido |
|---------|-----------|
| `src/main.py` | API FastAPI, configuración, arranque y apagado (`lifespan`) |
| `src/plataforma.py` | Cola, workers, cancelación, estadísticas, volcado a JSON |
| `src/tareas.py` | Las tres tareas y la validación de parámetros |
| `mediciones/medir.py` | Script que corre las tres mediciones contra la API |

---

## 3. Decisiones de diseño

### ¿Por qué `asyncio.Queue` y no `queue.Queue`?

`queue.Queue` está hecha para **threads**. Su `get()` bloquea el hilo hasta que
llega un elemento. Como todos los workers son corrutinas del mismo hilo, un
`get()` bloqueante congelaría el event loop entero: ni la API ni los demás
workers podrían avanzar. Además trae locks internos que acá no hacen falta.

`asyncio.Queue.get()` es una corrutina. `await cola.get()` suspende sólo a ese
worker y le devuelve el control al loop, que sigue atendiendo pedidos HTTP.
Cuando alguien hace `put`, el loop despierta a un worker que estaba esperando.

La cola tiene tamaño máximo (`TAMANO_COLA`). Si se llena, `put_nowait` lanza
`QueueFull` y la API responde `503`. Así, si llegan 10.000 tareas de golpe, no
crecen sin límite.

### ¿Cómo evito que una tarea CPU-bound congele la API?

`hashear` es una función común (sin `await`). Si se llama directo desde una
corrutina, el loop no puede cambiar a otra cosa hasta que termina. La medición 2
muestra que el `GET /tareas` tarda lo mismo que el hash.

La solución es `loop.run_in_executor(self.executor, tareas.hashear, ...)`. El
cálculo se manda a otro proceso y el worker hace `await` del futuro que
devuelve. Mientras tanto el loop queda libre.

Usé `ProcessPoolExecutor` y no threads (`asyncio.to_thread`) por el **GIL**: con
threads la API seguiría respondiendo, porque el GIL se va pasando entre threads,
pero dos hashes nunca correrían a la vez. Con procesos, cada uno tiene su propio
intérprete y su propio GIL, así que se usan todos los núcleos (medición 3b).
`main.py` tiene la guarda `if __name__ == "__main__"` porque en macOS los
procesos hijos arrancan con `spawn` y vuelven a importar el módulo.

### ¿Cómo implementé la cancelación, y qué pasa con los recursos a medio usar?

Cada tarea en ejecución corre en su **propia `asyncio.Task`**
(`plataforma.ejecutar`), guardada en `self.en_ejecucion[id]`. El worker la espera
con `await`. Así el `DELETE` puede cancelar la tarea sin cancelar al worker.

- **Pendiente**: se marca `cancelada`. Cuando un worker la saca de la cola, ve
  el estado y la saltea.
- **Ejecutando**: `DELETE` llama a `tarea_async.cancel()`. En el próximo
  `await` de la tarea se lanza `CancelledError`, que el worker atrapa y la
  marca `cancelada`. El id queda anotado en `cancelaciones_pedidas` para
  distinguir una cancelación pedida por la API (el worker sigue) de una
  cancelación del worker mismo durante el apagado (el `CancelledError` se
  vuelve a lanzar).

Qué pasa con los recursos según la tarea:

- `descargar`: el `CancelledError` aparece en el `await cliente.get(...)`.
  `asyncio.gather` cancela todas las descargas hermanas. El
  `async with httpx.AsyncClient()` cierra las conexiones al salir, aunque la
  salida sea por una excepción. No quedan sockets abiertos ni corrutinas
  huérfanas.
- `esperar`: se cancela el `asyncio.sleep` y listo.
- `hashear`: se cancela el futuro de asyncio y la tarea pasa a `cancelada`
  enseguida. Pero **el proceso hijo no se puede interrumpir** a mitad de un
  cálculo: sigue hasta terminar y el resultado se descarta (ver
  limitaciones).

El timeout funciona igual: `asyncio.wait_for` cancela la corrutina interna
cuando se vence el tiempo y lanza `TimeoutError`. La tarea queda `fallida` con
el motivo (`"timeout: superó los 5.0 segundos"`).

### ¿Qué pasa si un worker lanza una excepción no capturada?

Hay dos redes:

1. En `ejecutar`, `except Exception as error` atrapa cualquier error de la
   tarea. La tarea queda `fallida` con el tipo y el mensaje, y el worker sigue
   con la próxima.
2. En `worker`, otro `try/except Exception` alrededor de todo. Si falla algo de
   la propia plataforma, se loguea y el worker **no muere**. Si muriera, el pool
   quedaría con un worker menos sin que nadie se entere.

En los dos casos el `finally` llama a `cola.task_done()`. Si no, `cola.join()`
(que usa el apagado) quedaría esperando para siempre. `CancelledError` hereda de
`BaseException` y no de `Exception`, así que estos `except` no se tragan las
cancelaciones.

### Apagado ordenado con SIGTERM / SIGINT

uvicorn ya instala manejadores para `SIGINT` y `SIGTERM`. Al recibir una señal
deja de aceptar conexiones y ejecuta la parte del `lifespan` que está después
del `yield`, que llama a `plataforma.apagar()`:

1. `aceptando = False`. Si llega algún POST, responde `503`.
2. Las tareas que seguían en la cola se marcan `canceladas`.
3. Espera a las que están ejecutando con
   `wait_for(cola.join(), timeout=LIMITE_APAGADO)`.
4. Si se vence el límite, cancela las que quedan.
5. Cancela los workers (que a esta altura están esperando en `cola.get()`).
6. Cierra el executor (desde un thread, para no bloquear el loop) y vuelca todo
   a `estado_<timestamp>.json`.

No usé `loop.add_signal_handler()` porque reemplazaría el manejador de uvicorn y
el servidor nunca se enteraría de que tiene que cortar. Al arrancar,
`reportar_volcado_previo()` busca el último `estado_*.json` y loguea cuántas
tareas tenía y en qué estados.

En Docker: el `CMD` en forma de lista hace que Python sea el PID 1 y reciba el
`SIGTERM` de `docker compose stop` directamente. Además, `stop_grace_period: 20s`
es mayor que `LIMITE_APAGADO`, así Docker no lo mata con `SIGKILL` a mitad del
apagado.

### ¿Por qué `httpx` y no `aiohttp`?

Las dos son asíncronas. Elegí `httpx` porque su API es casi igual a la de
`requests` (`cliente.get(url)`, `respuesta.status_code`), así que el código se
lee fácil. Además tiene timeouts simples y se usa con `async with`, lo que
garantiza que se cierren las conexiones. También es la librería que usa FastAPI
en sus ejemplos de tests.

### Otras decisiones

- **Todos los endpoints son `async def`.** Si fueran `def`, FastAPI los
  correría en un thread aparte y tocarían `self.tareas` desde otro hilo, y ahí
  sí haría falta un Lock.
- **El estado es un diccionario `{id: dict}`** que se serializa a JSON tal cual.
  Lo interno (las `asyncio.Task` y las cancelaciones pedidas) va en estructuras
  aparte, para que el volcado no tenga objetos que no se pueden serializar.

---

## 4. Mediciones

Todas se hicieron con `mediciones/medir.py` contra la API corriendo **fuera de
Docker**: macOS, Python 3.14, 10 núcleos. Los tiempos de los lotes se calculan
con los timestamps del servidor (desde que se creó la primera tarea hasta que
terminó la última).

### Medición 1 — Concurrencia I/O: 20 descargas

20 URLs de `https://postman-echo.com/delay/1`, que tarda ~1 s en responder a
propósito. Se encargó una tarea `descargar` con `concurrente: false` (un `for`
con `await`) y otra con `concurrente: true` (`asyncio.gather`). Tres corridas:

| Corrida | Secuencial | Concurrente | Speedup |
|---------|-----------:|------------:|--------:|
| 1 | 24,33 s | 1,77 s | 13,7× |
| 2 | 24,21 s | 1,70 s | 14,2× |
| 3 | 24,17 s | 1,90 s | 12,7× |
| **Promedio** | **24,24 s** | **1,79 s** | **13,5×** |

**Interpretación:** en secuencial, cada descarga arranca recién cuando termina
la anterior, así que el tiempo es la **suma** (20 × ~1,2 s, contando la latencia
de red). Con `gather` las 20 corrutinas mandan su pedido y quedan suspendidas a
la vez, así que el tiempo es el de **la más lenta**. Durante esa espera la CPU
no hace nada: el event loop sólo espera que los sockets tengan datos. El
speedup no llega a 20× porque las 20 conexiones TLS también cuestan algo y no
todas las respuestas tardan lo mismo.

En la primera prueba usé `httpbin.org` y, en modo concurrente, algunas
respuestas vinieron con **429 Too Many Requests**: el servidor limita los
pedidos simultáneos. Por eso la cambié por postman-echo. La concurrencia también
tiene un techo del otro lado.

### Medición 2 — El event loop bloqueado

Se encarga un `hashear` de 30.000.000 iteraciones (~6 s) y, mientras corre, se
mide cada 0,25 s cuánto tarda en responder `GET /tareas` (más un
`GET /tareas/{id}` para saber cuándo terminó). Se corrió dos veces, cambiando
sólo `USAR_EXECUTOR`:

| | Sin executor (`USAR_EXECUTOR=false`) | Con `run_in_executor` |
|-|-:|-:|
| Duración del hash | 6,33 s | 6,88 s |
| GETs que se pudieron hacer durante el hash | **1** | **28** |
| Latencia máxima del GET | **6133,9 ms** | **3,8 ms** |
| Latencia promedio del GET | 6133,9 ms | 1,8 ms |

**Interpretación:** sin executor, el primer GET que llega mientras se calcula el
hash espera **6,1 segundos**: todo lo que faltaba del cálculo. No es que la API
esté lenta, es que **no corre**. `hashear` no tiene ningún `await`, así que el
loop nunca recupera el control para leer el socket del cliente. La conexión
queda esperando en el kernel sin que nadie la atienda. Por eso entró un solo GET
en todo el hash.

Con executor, el cálculo pasa a otro proceso y el loop sigue libre: 28 GETs con
menos de 4 ms cada uno, igual que sin carga. El hash tarda medio segundo más por
el costo de mandar el trabajo al otro proceso (y, la primera vez, de crearlo).
Ese medio segundo es el precio de tener la API viva.

### Medición 3 — Techo del pool

Hay dos techos distintos según el tipo de tarea, así que medí los dos y después
un tercero que apareció al subir mucho la cantidad.

**3a. Tareas I/O (`esperar 1s`) con 4 workers:**

| Tareas | Tiempo total |
|-------:|-------------:|
| 1 | 1,00 s |
| 2 | 1,01 s |
| 4 | 1,01 s |
| 8 | 2,01 s |
| 16 | 4,01 s |
| 32 | 8,01 s |

Hasta 4 tareas tardan lo mismo que una. Desde ahí el tiempo es exactamente
`⌈tareas / workers⌉ × 1 s`: las tareas de más esperan en la cola a que se
libere un worker. El límite es `CANT_WORKERS`. Para tareas I/O, subirlo es
barato, porque un worker parado en un `await` no consume CPU.

**3b. Tareas CPU (`hashear` 2.000.000 iteraciones) con 50 workers:**

| Hashes simultáneos | Tiempo total |
|-------------------:|-------------:|
| 1 | 0,58 s |
| 2 | 0,60 s |
| 4 | 0,65 s |
| 8 | 0,85 s |
| 10 | 0,86 s |
| 12 | 1,14 s |
| 16 | 1,31 s |
| 20 | 1,52 s |
| 30 | 2,33 s |
| 40 | 3,03 s |

Acá sobran workers, así que el techo no son ellos: son los **núcleos**. El
`ProcessPoolExecutor` crea por defecto `os.cpu_count()` procesos (10 en esta
máquina). Hasta 10 hashes el tiempo casi no cambia. Sube un poco desde 8,
probablemente porque no todos los núcleos de esta máquina rinden igual. Pasado
10, las tareas hacen fila en el executor y el tiempo crece lineal: 40 hashes ≈
4 tandas. Agregar workers de asyncio no ayuda con CPU-bound: lo que falta son
núcleos.

**3c. Muchas tareas I/O (`esperar 1s`) con 5000 workers:**

| Tareas | Tiempo total |
|-------:|-------------:|
| 100 | 1,05 s |
| 1000 | 3,40 s |
| 2000 | 3,98 s |
| 5000 | 9,04 s |

Con 100 tareas sigue siendo ~1 s. Con miles se degrada, aunque **cada tarea por
separado tardó 1,00 s** y la espera promedio en cola fue 0 s (según
`/estadisticas`). El tiempo extra no está en los workers: es que todo pasa por
**un solo hilo**. El mismo event loop tiene que parsear miles de POSTs, armar las
respuestas JSON y atender los GETs con los que el script consulta el estado
cada 0,2 s. Es una sola CPU, y se satura. Las tareas se van creando más
despacio de lo que se ejecutan.

**¿Dónde está el límite y por qué?**

- I/O-bound: primero, la cantidad de workers (3a). Si se suben, el siguiente
  límite es el propio event loop, que procesa todo el HTTP en un único núcleo
  (3c). Y del lado de afuera, el servidor remoto también limita (los 429 de la
  medición 1).
- CPU-bound: la cantidad de núcleos (3b). El GIL impide usar threads y los
  procesos no pueden ser más que los núcleos sin hacer fila.

---

## 5. Conceptos del curso aplicados

- **Productor-consumidor (clase 11)**: la API produce ids con
  `cola.put_nowait()` y los workers los consumen con `await cola.get()`. Es el
  mismo patrón que con threads, pero con corrutinas. No hace falta lock porque
  el event loop no interrumpe a una corrutina entre `await`s: por ejemplo,
  `encolar()` crea la tarea y la guarda en `self.tareas` sin ningún `await` en
  el medio, así que nadie puede verla a medio armar.
- **Sockets y servidores concurrentes (clases 13-14)**: uvicorn es un servidor
  TCP que atiende muchos clientes a la vez en un solo hilo. A diferencia de un
  servidor con un thread o un fork por conexión, cada cliente es una corrutina.
- **I/O multiplexing (clase 17)**: el event loop de asyncio, por dentro, usa
  `select`/`epoll` (`kqueue` en macOS). Cuando todas las corrutinas están en un
  `await` (esperando un socket, un `sleep` o la cola), el loop se queda
  bloqueado en el `select` hasta que algún descriptor tenga datos. Por eso 20
  descargas o 5000 `esperar` no consumen CPU mientras esperan.
- **De yield a asyncio (clase 18)**: una corrutina es un generador con otro
  nombre. Cada `await` es un punto donde la función se suspende y le devuelve
  el control al loop, como `yield`. Por eso **`time.sleep()` en una corrutina
  es un bug**: duerme el hilo entero sin devolver el control, igual que el hash
  de la medición 2. `asyncio.sleep()` en cambio suspende sólo esa corrutina.
  En este TP no hay ningún `time.sleep()`.
- **HTTP y FastAPI (clase 19)**: la API con modelos de Pydantic para validar el
  cuerpo (`NuevaTarea`), códigos de estado (202, 400, 404, 409, 503) y el
  `lifespan` para arranque y apagado. El `lifespan` es un context manager hecho
  con `@asynccontextmanager` (Bloque 0).
- **Asyncio en red (clase 20)**: `httpx.AsyncClient` para las descargas.
  Medición 1: **`asyncio.gather` contra un `for` con `await`**. El `for` espera
  que termine cada descarga antes de empezar la siguiente. `gather` las lanza
  todas y espera a que terminen juntas.
- **Asyncio avanzado (clase 21)**: `asyncio.Queue`, `create_task`, `cancel()` y
  `CancelledError` para la cancelación, `wait_for` para los timeouts,
  `cola.join()`/`task_done()` para el apagado, y `run_in_executor` /
  `to_thread` para sacar trabajo bloqueante del loop.
- **`concurrent.futures` (clase 22)**: `ProcessPoolExecutor` para `hashear`.
  `run_in_executor` envuelve el `Future` de `concurrent.futures` en uno de
  asyncio, que se puede esperar con `await`.

### ¿Dónde podría bloquearse el event loop?

- En el modo `USAR_EXECUTOR=false`, que existe a propósito para la medición 2.
- `volcar_estado()` y `reportar_volcado_previo()` escriben y leen el JSON de
  forma síncrona. Pasan una sola vez, al apagar y al arrancar, cuando no se
  atienden pedidos.
- `GET /tareas` y `/estadisticas` recorren todas las tareas. Con decenas de
  miles, armar esa respuesta ya se nota en el loop (parte de lo que se ve en la
  medición 3c).

---

## 6. Limitaciones conocidas

- **Un `hashear` cancelado (o vencido) sigue calculando** en su proceso hasta
  terminar. La tarea figura como `cancelada`/`fallida` y el resultado se
  descarta, pero mientras tanto ocupa un proceso del executor y demora el
  apagado: en una prueba el apagado tardó ~3 s más esperando a ese proceso. Un
  `Future` que ya está corriendo no se puede interrumpir.
- **Todo en un proceso**: el HTTP y la coordinación de todas las tareas pasan
  por un único hilo, y ese es el techo para cargas muy grandes (medición 3c).
- **La memoria crece sin límite**: las tareas terminadas nunca se borran de
  `self.tareas`. `GET /tareas` devuelve todo, sin paginación.
- **Al reiniciar no se recuperan las tareas**: el volcado sólo se reporta por
  log, no se vuelve a cargar.
- **Si un proceso del executor muere** (por ejemplo, si el sistema lo mata), el
  `ProcessPoolExecutor` queda roto (`BrokenProcessPool`) y todos los `hashear`
  siguientes fallan hasta reiniciar.
- **Durante el apagado**, uvicorn cierra el socket antes de llamar a
  `apagar()`. En la práctica un POST nuevo recibe "connection refused" más que
  el `503`.
- Sin autenticación ni límite de tareas por cliente.
- Las mediciones son de macOS fuera del contenedor. Dentro de Docker,
  `os.cpu_count()` devuelve los núcleos asignados a la VM de Docker, así que el
  techo de la medición 3b puede ser otro.

---

## 7. Lo que aprendí

Lo que más me sirvió fue la medición del event loop bloqueado. Sabía que asyncio
"no sirve para CPU-bound", pero no terminaba de entender por qué hasta que vi que
un `GET /tareas` tardaba 6 segundos en responder mientras corría un hash. Y no
era lenta: la API directamente no se ejecutaba, porque una función sin `await`
nunca le devuelve el control al loop. Ahí me cerró que una corrutina es parecida
a un generador: sólo cede el control en los puntos donde se suspende, y si no hay
ninguno, nadie más puede avanzar. Por eso `time.sleep()` dentro de una corrutina
es un error y `asyncio.sleep()` no.

También entendí que cada tipo de tarea tiene su propio techo. Para las tareas de
I/O, sumar workers es casi gratis, porque una corrutina que espera un socket no
gasta CPU. Pasar de 24 a menos de 2 segundos en las 20 descargas sin agregar
ningún thread muestra lo que el event loop hereda de `select`/`epoll`. Para el
hash, en cambio, agregar workers no cambiaba nada: el límite eran los núcleos, y
por el GIL hubo que usar procesos y no threads. Y cuando subí a miles de tareas
apareció un tercer límite que no esperaba: el único hilo que atiende todo el
HTTP. Medir me mostró cosas que no hubiera supuesto, como los errores 429 del
servidor remoto cuando le pedía 20 descargas a la vez.

Por último, aprendí que cancelar bien es más difícil que ejecutar. Hubo que
separar cada tarea en su propia `asyncio.Task` para poder cancelarla sin matar al
worker, distinguir quién pidió la cancelación, y asegurarse de que `task_done()`
se llame siempre para que el apagado no quede colgado. También descubrí que hay
cosas que no se pueden cancelar: un hash que ya está corriendo en otro proceso
sigue hasta terminar. Me quedó claro que en programación concurrente hay que
pensar tanto en cómo terminan las tareas como en cómo empiezan.
