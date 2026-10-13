# Contexto del proyecto AOG AutoRelog (para asistentes de IA)

Pegá este documento al inicio de una conversación con otra IA para que entienda el proyecto. Está escrito para ser autocontenido.

## 1. Qué es
Aplicación de escritorio **solo para Windows** (Python 3.10+, tkinter, OpenCV, pywin32, psutil, numpy) que acompaña al juego **Angels Online Global** (proceso `angel.dat`). Hace tres cosas:

1. **Relog automático:** cuando se corta la conexión y el juego muestra "Connection to server has been interrupted", recorre las pantallas (desconexión → servidor → canal → PIN → anuncio → selección de personaje → mundo → aviso de Trade PIN), y al volver activa el **Angel Baby** (auto-batalla del juego: Ctrl+R y tildar *Attack*). No guarda contraseñas: usa la sesión que el cliente ya tiene (login con Google).
2. **Monitor de internet** con informes (HTML/CSV) para reclamar al proveedor.
3. **Lector de experiencia** del HUD con ritmo por hora y aviso de estancamiento.

Repositorio: https://github.com/joacoz29/AOGAutoRelog. Documentación detallada en `docs/` (ARQUITECTURA, FUNCIONAMIENTO, DATOS, EXPERIENCIA, CONFIGURACION, DESARROLLO). Idioma del proyecto: español rioplatense.

**Restricciones de diseño importantes:** no inyecta código, no lee la memoria del juego, no modifica archivos del cliente. Solo (a) captura la ventana y (b) le envía mensajes de entrada. Es automatización de un juego online: puede ir contra sus términos de servicio; el usuario lo sabe.

## 2. Archivos
| Archivo | Rol |
|---|---|
| `autorelog.py` | Motor. Clase `Relog` (máquina de estados, `step()`), captura, reconocimiento por plantillas, clicks/teclas, Angel Baby. Corre también por consola. |
| `gui.py` | Interfaz tkinter (pestañas: Auto Relog, Experiencia, Conexión, Ajustes), bandeja, instancia única. |
| `netmon.py` | Monitor de internet (hilo propio), estadísticas, informes. |
| `expmon.py` | Lectura de `NN.NN %` del HUD con prototipos de dígitos + estadísticas de ritmo. |
| `tray.py` | Icono de bandeja con pywin32. |
| `templates/*.png`, `templates/hd/*.png`, `templates/exp_digits.npz` | Imágenes de referencia (dos estilos de dibujo) y prototipos de dígitos. |
| `tools/train_digits.py`, `tools/simulate_relog.py` | Entrenar dígitos / simular un relog completo con capturas. |
| `config.json` (local, no versionado) | Ajustes. Valores por defecto en `DEFAULTS` de `autorelog.py`. |
| `datos/` (local) | `cortes.jsonl`, `latencia.csv`, `sesiones.jsonl`, `juego.jsonl`, `exp.csv`. |

Dependencias entre módulos: `gui` importa a todos; `autorelog` importa `netmon` y `expmon`; `netmon`, `expmon` y `tray` no importan otros módulos del proyecto.

## 3. Cómo funciona (lo esencial)
- **Captura:** `PrintWindow(hwnd, ..., 2)` del área cliente. Funciona con la ventana tapada, **no** minimizada (devuelve vacío).
- **Reconocimiento:** `cv2.matchTemplate` (TM_CCOEFF_NORMED, umbral 0.8) sobre gris. El juego dibuja su interfaz **1:1 en cualquier resolución**, así que se busca a escala 1.0 (solo si no se reconoce nada ~4 s se prueban 11 escalas).
- **Dos estilos de dibujo:** `sd` (original) y `hd` (texto nítido; aparece al usar el "Render Fix" de otra herramienta que parchea `angel.dat`). Cada plantilla puede existir en `templates/<n>.png` y `templates/hd/<n>.png`; `find()` prueba primero el estilo que coincidió la última vez. Las **distancias** a botones también dependen del estilo (`CHANNEL_LAYOUT`, `AB_LAYOUT`).
- **Reglas** (`RULES`, en orden de prioridad; la primera que coincide gana): `disconnect`, `pin`, `tradepin`, `announcement`, `server`, `channel`, `character`. Cada una tiene marcador (texto) y botón buscado debajo. Dentro del mundo solo se evalúan `disconnect` y `tradepin`.
- **Estado "en el mundo":** el título de la ventana termina en `(Servidor-Canal)`. En el login es solo `Angels Online Global`.
- **Entrada en segundo plano:** `SendMessageTimeout` con mensajes `WM_MOUSEMOVE/LBUTTON*` y `WM_KEYDOWN/UP`. Antes de cada entrada se "despierta" al juego (`WM_ACTIVATEAPP`, `WM_NCACTIVATE`, `WM_ACTIVATE`, `WM_SETFOCUS`) porque el cliente ignora la entrada si cree que no está activo; se lo vuelve a "dormir" al terminar la secuencia (no antes: procesa la entrada en su siguiente cuadro). Pulsaciones largas (click 0.4 s en el login, tecla 0.3 s) porque lee la entrada por cuadro. El cursor del juego se estaciona en `PARK=(900,30)` tras cada click (si no, tapa las plantillas).
- **Escalado de método de click** por regla: click largo → click largo → mouse real (solo ese click). `MIN_REPEAT` evita repetir antes de que cargue la pantalla. Freno: `max_attempts` en 60 s → pausa 60 s.
- **Canal:** se verifica por color que la fila elegida quedó marcada (la marcada es rojiza); si no, reintenta y **no confirma** (confirmar sin elegir entra al canal por defecto). Al entrar compara el canal del título con el elegido y no guarda uno distinto como preferido.
- **Juego minimizado / corte:** si el internet lleva `peek_during_outage_s` caído o vuelve, restaura el juego; con `outage_front` lo deja **al frente** (sin volver a minimizar), si no, de fondo. La revisión periódica restaura y vuelve a minimizar.
- **Monitor de internet:** 1 prueba/s, 3 conexiones TCP en paralelo (1.1.1.1:443, 8.8.8.8:53, 9.9.9.9:443); corte = fallan los 3 durante ≥2 pruebas; el inicio se fecha en la primera falla. Causa orientativa por ping al router (responde → proveedor; no → red local). Suspensión de la PC (>20 s sin medir) no cuenta como corte.
- **Experiencia:** texto `NN.NN %` en fuente de píxeles (~4×7). Tinta = brillo ≥100 y saturación ≤70; se separan caracteres por columnas y se clasifican contra prototipos (tolera 1 px). La posición se **calibra sola** por tamaño de ventana. Baja <0.30 % = ruido; salto grande pide 2 lecturas iguales; baja >50 % = subida de nivel.
- **Hilos:** principal (tkinter), worker (`Relog.step()` bajo `Relog._lock`), `netmon`, `tray`, hilos de un solo uso (botones). Los hilos no tocan widgets: usan colas.

## 4. Cosas que costaron descubrir (no repetir)
- El cliente descarta clicks cortos en el login y tecla/entrada si cree estar inactivo.
- El cursor dibujado por el juego tapa plantillas → estacionarlo y tener una marca de respaldo (`find_ab`).
- Un click perdido en la fila del canal deja el canal por defecto (el 1, suele estar lleno).
- Tras reloguear, el cartel de desconexión a veces aparece **decenas de segundos después** de volver internet.
- Plantillas con casillas tildadas pierden coincidencia al cambiar el estado: recortar solo texto.
- Capturar con el juego minimizado devuelve imagen vacía (hay que restaurarlo sin foco con `SW_SHOWNOACTIVATE`).
- Pruebas que mandan clicks a un juego real afectan a esa sesión.
- Escribir `config.json` mientras hay una instancia abierta: la instancia lo pisa.

## 5. Cómo probar
- Reconocimiento sobre una captura: `python autorelog.py --image captura.png`.
- Relog completo simulado: `python tools/simulate_relog.py <carpeta> [--lose-start]` (tiempo simulado, juego falso).
- El registro `autorelog.log` tiene hora con milisegundos y el método de click usado; es la mejor fuente para depurar.

## 6. Estado actual
- Funciona en una desconexión real con el estilo `hd` (1920×1032): ~16–22 s del cartel al mundo, sin mouse real, Angel Baby activado.
- Pendiente / límites: el click de *Start* a veces se pierde (se reintenta rápido, a ~1 s, comparando la captura con la de antes del click: `server_early_probe`); si el canal elegido está lleno solo lo avisa; no prueba un canal alternativo; la lectura de experiencia necesita el juego visible (no minimizado); los prototipos de dígitos de algunos números tienen pocos ejemplos; sin licencia en el repo.

## 7. Qué ayudaria que una IA que ayude con este proyecto

- Agregar automatización de combate o economía más allá del Angel Baby que ya viene en el juego.
- Antes de tocar el motor, correr `tools/simulate_relog.py` y revisar `autorelog.log`.
