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

## Plantillas (`templates/`)

Son recortes en escala de grises de la interfaz del juego **con la ventana a 1366×768 (área cliente)**.

| Archivo | Qué es |
|---|---|
| `disc_text`, `pin_text`, `tradepin_text` | Texto del cartel (marcador). |
| `server_hdr`, `channel_hdr` | Cabecera de *Select Server* / *Select Channel*. |
| `ok_btn`, `tradepin_no`, `confirm_btn`, `start_btn`, `chan_ok`, `enter_btn` | Botones. |
| `ab_enable`, `ab_attack`, `ab_tabs` | Marcas del panel del Angel Baby. |

Cómo hacer o actualizar una:

1. Capturá la ventana a escala 1:1 (o usá `autorelog.grab(hwnd)` y guardá el resultado con `cv2.imwrite`).
2. Recortá **solo lo que no cambia**. Evitá incluir: el cursor, bordes de *hover*, casillas tildadas/destildadas y fondos que se ven a través de un panel semitransparente.
3. Guardá como `templates/<nombre>.png` y comprobá con `--image`.

Reglas prácticas:

- Un marcador de **texto** es más estable que un botón (el botón cambia con el *hover*).
- Una plantilla que incluya una casilla pierde coincidencia al cambiar su estado: por eso `ab_enable` recorta solo el texto, sin la casilla.
- Las posiciones de los botones se expresan **relativas al marcador**, no absolutas (ver `CHANNEL_*` y `AB_*` en `autorelog.py`).

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
| Reconocer tardaba ~1.7 s | Búsqueda en 11 escalas | Una sola escala cuando la ventana mide 1366×768. |
| El programa "no hacía nada" tras Detener/Iniciar | La pausa de 60 s sobrevivía al reinicio | `Relog.reset()` al iniciar. |
| Se perdían datos de config tras un corte de luz | Escritura no atómica | `os.replace` sobre un temporal y copia `.bak`. |
| El juego aparecía solo al minimizarlo | Un pedido de revisión viejo seguía vigente | Descartarlo mientras el juego está visible. |

Otras advertencias:

- **Probar contra un juego real afecta a esa sesión.** Cualquier test que mande clicks puede tocar el personaje; conviene probar sobre elementos inocuos (pestañas del panel, casillas que se restauran).
- **No busques la ventana de la bandeja por su nombre en las pruebas**: puede ser la de una instancia real en ejecución. Usá `app.tray.hwnd`.
- Las pruebas que reescriben `config.json` mientras hay una instancia abierta serán pisadas por esa instancia.

## Estilo

- Comentarios y mensajes en español (rioplatense en los textos que ve el usuario).
- Las funciones del motor que **actúan sobre el juego** son `bg_click`, `bg_keys`, `real_click` y `Relog.do`; todo lo demás observa.
- La interfaz nunca debe bloquearse esperando al juego ni a la red: lo lento va en hilos.
