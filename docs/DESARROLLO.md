# Guía de desarrollo

## Entorno

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python.exe gui.py          :: interfaz con consola de errores
.venv\Scripts\python.exe autorelog.py    :: solo el motor, por consola
```

`AutoRelog.bat` usa `pythonw.exe` (sin consola). Para ver errores mientras desarrollás, ejecutá con `python.exe`.

Dependencias: `opencv-python`, `numpy`, `psutil`, `pywin32`. tkinter viene con Python.

## Probar sin el juego

El motor se puede probar contra una captura estática:

```bat
.venv\Scripts\python.exe autorelog.py --image captura.png
```

Imprime qué regla coincide (con y sin "estar en el mundo"), el puntaje, la escala y la posición. Es la forma rápida de comprobar una plantilla nueva.

Para probar el monitor de internet **sin ensuciar tus datos reales**, redirigí las rutas del módulo a una carpeta temporal antes de crear el monitor:

```python
import tempfile, pathlib, netmon as n
tmp = pathlib.Path(tempfile.mkdtemp())
n.DATA = tmp
n.F_CUTS, n.F_LAT, n.F_SES, n.F_GAME, n.F_STATE = (tmp / x for x in
    ("cortes.jsonl", "latencia.csv", "sesiones.jsonl", "juego.jsonl", "estado.json"))
```

Un corte se puede simular cambiando `netmon.TARGETS` por direcciones que no contestan (p. ej. `("10.255.255.1", 81)`) y restaurándolo después.

## Simular un relog completo sin el juego ni la red

`tools/simulate_relog.py` reproduce un relog con capturas reales: un "juego falso" cambia de pantalla según dónde se haga click y se mide qué hace el motor (clicks, tiempos, reintentos). El tiempo es simulado, así que no espera de verdad.

```bat
.venv\Scripts\python.exe tools\simulate_relog.py carpeta_con_capturas [--lose-start]
```

La carpeta necesita `disc, server, chan1, chan4, pin, ann, char, trade, world` (`.png`, pantalla completa 1920×1032). Con `--lose-start` el primer click de *Start* se pierde, para comprobar el reintento. Sirve para validar un cambio en el motor o en las plantillas antes de probarlo con un corte real.

## Plantillas (`templates/`)

Son recortes en escala de grises de la interfaz del juego (área cliente, a escala 1:1). Cada nombre puede tener **dos versiones**: `templates/<nombre>.png` (estilo original) y `templates/hd/<nombre>.png` (texto nítido del *Render Fix*); ver [FUNCIONAMIENTO.md](FUNCIONAMIENTO.md#estilos-de-dibujo-del-juego).

| Archivo | Qué es |
|---|---|
| `disc_text`, `pin_text`, `tradepin_text` | Texto del cartel (marcador). |
| `server_hdr`, `channel_hdr` | Cabecera de *Select Server* / *Select Channel*. |
| `ok_btn`, `tradepin_no`, `confirm_btn`, `start_btn`, `chan_ok`, `enter_btn` | Botones. |
| `ab_enable`, `ab_attack`, `ab_tabs` | Marcas del panel del Angel Baby. |
| `exp_digits.npz` | Prototipos de los dígitos del HUD (no es una imagen): se generan con `tools/train_digits.py`. Ver [EXPERIENCIA.md](EXPERIENCIA.md). |

Cómo hacer o actualizar una:

1. Capturá la ventana a escala 1:1 (o usá `autorelog.grab(hwnd)` y guardá el resultado con `cv2.imwrite`).
2. Recortá **solo lo que no cambia**. Evitá incluir: el cursor, bordes de *hover*, casillas tildadas/destildadas y fondos que se ven a través de un panel semitransparente.
3. Guardá como `templates/<nombre>.png` y comprobá con `--image`.

Reglas prácticas:

- Un marcador de **texto** es más estable que un botón (el botón cambia con el *hover*).
- Una plantilla que incluya una casilla pierde coincidencia al cambiar su estado: por eso `ab_enable` recorta solo el texto, sin la casilla.
- Las posiciones de los botones se expresan **relativas al marcador**, no absolutas (ver `CHANNEL_*` y `AB_*` en `autorelog.py`).

## Armar las plantillas de un estilo nuevo (por ejemplo `hd`)

Las pantallas del login solo aparecen cuando se corta la conexión, así que el programa **guarda solo las que no reconoce**:

- Con `capture_unknown` (activado por defecto) escribe en `capturas/` (ignorada por git) cada pantalla del login no reconocida, y los carteles del mundo no reconocidos durante un corte largo o justo al entrar tras reloguear. Guarda como máximo una cada 2 s y solo si cambió respecto de la anterior; tope de 40 por inicio.
- Con esas capturas se recortan las plantillas en `templates/hd/` (mismos nombres) y se miden sus distancias (`CHANNEL_LAYOUT["hd"]`, `AB_LAYOUT["hd"]`) respecto de la esquina de cada plantilla.
- El botón **Capturar** de la pestaña principal guarda al instante la pantalla actual (`<hora>_manual.png`): sirve para las pantallas que pasan rápido o que no se registraron solas.
- Las capturas contienen el nombre de tu cuenta (se ve en el login): revisalas antes de compartirlas y borrá la carpeta cuando termines.

## Agregar una pantalla o cartel nuevo

1. Creá las plantillas (marcador y, si hace falta, botón).
2. Agregá una entrada a `RULES` en `autorelog.py`, **en el orden de prioridad correcto** (los carteles que tapan otras pantallas, antes):

   ```python
   dict(name="mi_cartel", marker="mi_cartel_text", button="ok_btn", world=False)
   ```

   `world=True` hace que también se evalúe dentro del mundo.
3. Si necesita más de un click o una posición calculada, tratalo como `channel` en `Relog.do()`.
4. Si debe esperar más antes de repetirse, agregalo a `MIN_REPEAT`.
5. Agregá su texto a `PHASE_TEXT` / `PHASE_TO_STEP` en `gui.py` si querés que se vea en la interfaz.

## Lecciones aprendidas (trampas conocidas)

Cosas que costó descubrir y que conviene no repetir:

| Síntoma | Causa | Qué se hace |
|---|---|---|
| El juego ignora clicks y teclas con otra ventana al frente | El cliente descarta la entrada si cree que no está activo | Mandarle los mensajes de activación antes de cada entrada (`_wake`). |
| `Ctrl+R` "no abría" el panel a veces | El **cursor que dibuja el juego** quedaba encima del texto usado para reconocer el panel y la coincidencia caía | Estacionar el cursor (`PARK`) tras cada click y tener una segunda marca de respaldo (`find_ab`). |
| La tecla enviada se perdía | Se "dormía" al juego antes de que procesara la entrada (la lee en su siguiente cuadro) | Mantener la ventana despierta durante la secuencia y dormirla al terminar. |
| Clicks que no cerraban carteles del login | A pocos FPS una pulsación corta cae entre dos cuadros | Pulsación más larga y, si no alcanza, mouse real solo para ese paso. |
| Reconocer tardaba ~1.7 s | Búsqueda en 11 escalas | Una sola escala (la interfaz es 1:1 en cualquier resolución); las demás solo si hace falta. |
| Tras aplicar el Render Fix dejó de reconocer el panel del Angel Baby y de leer la experiencia | El texto se dibuja con otro estilo (trazos finos) y el HUD cambia de lugar con la resolución; las imágenes de referencia eran del estilo anterior | Plantillas `hd/` con sus propias distancias (`AB_LAYOUT`), prototipos de dígitos nuevos y calibración automática del texto de la experiencia. |
| El programa "no hacía nada" tras Detener/Iniciar | La pausa de 60 s sobrevivía al reinicio | `Relog.reset()` al iniciar. |
| Se perdían datos de config tras un corte de luz | Escritura no atómica | `os.replace` sobre un temporal y copia `.bak`. |
| El juego aparecía solo al minimizarlo | Un pedido de revisión viejo seguía vigente | Descartarlo mientras el juego está visible. |
| Cada pantalla del login tardaba ~4 s de más | El primer click (corto) nunca registraba; el segundo (largo) sí | Empezar por el click largo. Se vio leyendo `autorelog.log`: conviene revisar el registro antes de suponer. |
| El click de *Start* perdido costaba ~3 s | El reintento respetaba `MIN_REPEAT["server"]`=3 s aunque la pantalla no hubiera cambiado | Comparar la captura con la de **antes** del click a los 1 s (`server_early_probe`): si es idéntica, el click se perdió y se reintenta ya. Si cambió algo, se asume carga y no se reintenta (evita clicks de más en una transición). |
| El relog terminaba en el canal 1 en vez del 4 | El click en la fila se perdía y el ✔ aceptaba la fila marcada por defecto | Medir el color de la fila tras clickear y reintentar; no aceptar si no quedó elegida. |
| El canal equivocado se volvía el "preferido" | Se guardaba el canal del título sin importar cómo se llegó | No guardarlo si difiere del elegido en el relog. |

Otras advertencias:

- **Probar contra un juego real afecta a esa sesión.** Cualquier test que mande clicks puede tocar el personaje; conviene probar sobre elementos inocuos (pestañas del panel, casillas que se restauran).
- **No busques la ventana de la bandeja por su nombre en las pruebas**: puede ser la de una instancia real en ejecución. Usá `app.tray.hwnd`.
- Las pruebas que reescriben `config.json` mientras hay una instancia abierta serán pisadas por esa instancia.

## Estilo

- Comentarios y mensajes en español (rioplatense en los textos que ve el usuario).
- Las funciones del motor que **actúan sobre el juego** son `bg_click`, `bg_keys`, `real_click` y `Relog.do`; todo lo demás observa.
- La interfaz nunca debe bloquearse esperando al juego ni a la red: lo lento va en hilos.
