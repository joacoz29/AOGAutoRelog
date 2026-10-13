# Cambio: reintento rápido del click de *Start* (para revisión de otra IA)

Este documento describe un cambio hecho en `autorelog.py` del proyecto **AOG AutoRelog**.
Está pensado para que otra IA (Claude) lo revise y opine. Incluye el contexto, el diagnóstico,
el diseño, **todo el código agregado** y los resultados de las pruebas.

- Archivo modificado (motor): `autorelog.py`
- Documentación actualizada: `docs/CONFIGURACION.md`, `docs/FUNCIONAMIENTO.md`, `docs/DESARROLLO.md`, `docs/CONTEXTO_IA.md`
- No se tocó la interfaz (`gui.py`) ni el resto de módulos.
- No se commiteó nada.

---

## 1. Contexto

AutoRelog reconoce la pantalla del juego por plantillas (OpenCV) y hace clicks en segundo plano
(`SendMessage`) para reloguear. El flujo del login es:

```
disconnect -> server (botón Start) -> channel -> pin -> announcement -> character -> mundo
```

Reglas relevantes:

- `MIN_REPEAT` (segundos antes de repetir el mismo click): `server` **3.0**, `channel` 4.0,
  `character` 4.5, `disconnect` 3.0, `pin` 2.5, `announcement` 2.5, resto 1.5. Solo se repite si la
  pantalla **sigue siendo la misma**.
- Tras un click exitoso, el juego tarda en mostrar la pantalla siguiente (p. ej. después de *Start*
  tardó **~2.35 s** en aparecer *Select Channel*, medido en `autorelog.log`).

## 2. Problema observado

El click de *Start* (pantalla *Select Server*) **a veces se pierde justo cuando aparece la pantalla**.
Era un "pendiente / límite" ya documentado en el proyecto: *"el click de Start a veces se pierde
(se reintenta a los 3 s; causa no confirmada)"*.

Cada *Start* perdido costaba **~4.3 s** de más porque el reintento respetaba `MIN_REPEAT["server"]=3`.

### Diagnóstico (evidencia real, `autorelog.log`)

Ejemplos de dos relogueadas donde el *Start* se perdió (primer click a las `:56.946`, segundo a las `:01.280`):

```
13:57:56.946 hago: server -> click (688,623) [start_btn 1.00] [fondo]
13:58:01.280 hago: server -> click (688,623) [start_btn 1.00] [fondo largo]
```

Diferencia = 4.33 s. El primer click no cambió la pantalla, y hubo que esperar ~3 s de `MIN_REPEAT`
más el cooldown. En un relog exitoso, tras el *Start* la pantalla siguiente apareció **2.35 s**
después, así que no se puede simplemente bajar `MIN_REPEAT` (se clickearía durante la carga).

## 3. Idea de la solución (discriminador)

Tras clickear *Start*, **guardar la captura de la pantalla justo antes del click**. A los
`server_early_probe` segundos comparar con la captura actual:

- **Idéntica** (nada cambió) -> el click se perdió -> reintentar **ya**, sin esperar `MIN_REPEAT`.
- **Cambió aunque sea mínimamente** -> la pantalla está cargando (transición) -> **no** hacer nada.

Se usa la **diferencia máxima** sobre una firma reducida (96×54, escala de grises) con umbral `> 6`,
en lugar del promedio: el promedio es poco sensible cuando el movimiento ocupa poca área y producía
reintentos falsos durante la carga (ver pruebas).

Además:

- Solo se aplica con el juego visible y **fuera del mundo** (`not in_world`), y dentro de una ventana
  de 6 s tras el click (`server_probe_until`).
- El método de click del reintento escala como el motor: `fondo -> fondo largo -> mouse real`
  (nivel 1, sube con `act_hist`).
- Se **re-arma** tras cada reintento, por si se pierde de nuevo.
- `_srv_base` se consume (se pone a `None`) apenas se decide, para no reusar una firma vieja.

## 4. Código agregado (autorelog.py)

### 4.1 Nueva opción en `DEFAULTS`

```python
"threshold": 0.8,
"server_early_probe": 1.0,  # s tras clickear Start antes de mirar si la pantalla cambio (carga); si no cambio, reintenta sin esperar el MIN_REPEAT
"dismiss_trade_pin": True,
```

### 4.2 Estado nuevo en `Relog.__init__` (junto a `self.ab_tries = 0`)

```python
self.server_retry_at = 0.0     # tras clickear Start: piso antes de volver a mirar si avanzo (reintento rapido)
self.server_probe_until = 0.0  # fin de la ventana de reintento rapido tras un Start
self._srv_base = None          # cuadro de Select Server justo antes del click con el que se compara
```

### 4.3 Reset (al reiniciar la vigilancia)

```python
self.world_since = None
self.server_retry_at = self.server_probe_until = 0.0
self._srv_base = None
```

### 4.4 Método nuevo `_server_stuck` (justo antes de `_exp_tick`)

```python
def _server_stuck(self, gray):
    """True solo si tras clickear Start la pantalla de *Select Server* sigue igual que ANTES del
    click (pixel a pixel, casi). Tras un Start exitoso el juego tarda ~2.4 s en mostrar el canal
    y durante ese rato cambia aunque sea un poco (fundido de `transition`, cursor); si nada cambio
    el click no registro y conviene reintentar ya, sin esperar el MIN_REPEAT."""
    if self._srv_base is None or find(gray, "server_hdr", self.cfg["threshold"]) is None:
        return False
    sig = cv2.resize(gray, (96, 54), interpolation=cv2.INTER_AREA).astype(np.int16)
    changed = int(np.abs(sig - self._srv_base).max()) > 6   # cualquier zona que se mueva = transicion
    self._srv_base = None
    return not changed   # identica a antes del click: el click no registro
```

### 4.5 Bloque nuevo en `_step` (después de calcular `gray`, antes de `hit = detect(...)`)

```python
# Start perdido: si venimos de clickear en Select Server y a los ~1 s la pantalla sigue identica a
# la de antes del click, reintentar sin esperar el MIN_REPEAT (que cuesta ~3 s de mas). Si algo
# cambio (transicion/carga) no se toca, para no clickear encima de una pantalla que si avanzo.
if (self.server_probe_until and time.time() < self.server_probe_until and not in_world
        and time.time() >= self.server_retry_at and self._server_stuck(gray)):
    rule = next(r for r in RULES if r["name"] == "server")
    self.info["phase"] = "server"
    self.next_delay = self.cfg["fast_poll"]
    if not online():
        self.info["phase"] = "offline"
        self.status("sin internet; espero a que vuelva")
        self.next_delay = 1.0
        return
    btn = find(gray, "start_btn", self.cfg["threshold"])
    if btn:
        self.status("Start no avanzó; reintento")
        self.do(hwnd, rule, btn, gray, 1)
        self.act_hist["server"] = [t for t in self.act_hist.get("server", [])
                                   if time.time() - t < 20] + [time.time()]
        self.last_act["server"] = time.time()
        self.info["acted"] += 1
        self.awake = True
        # re-armar por si este reintento tambien se pierde
        self.server_retry_at = time.time() + self.cfg.get("server_early_probe", 1.0)
        self.server_probe_until = time.time() + 6.0
        self._srv_base = cv2.resize(gray, (96, 54), interpolation=cv2.INTER_AREA).astype(np.int16)
        self.last_status = None
        time.sleep(self.cfg["action_cooldown"])
    return
```

Nota: `self.server_retry_at = 0.0` (que estaba en un borrador) se quitó; el re-armado lo maneja el
bloque de arriba. El piso temporal (`>= server_retry_at`) lo cumplen los `fast_poll` de 0.35 s, ya que
la ventana efectiva se abre `server_early_probe` (1.0 s) después del click.

### 4.6 Armado de la ventana tras el click normal (en el bloque de acción, junto a `name == "character"`)

```python
if name == "server":   # ventana de reintento rapido por si el click se pierde
    self.server_retry_at = time.time() + self.cfg.get("server_early_probe", 1.0)
    self.server_probe_until = time.time() + 6.0
    self._srv_base = cv2.resize(gray, (96, 54), interpolation=cv2.INTER_AREA).astype(np.int16)
```

## 5. Documentación actualizada

- `docs/CONFIGURACION.md`: nueva fila `server_early_probe` (por defecto `1.0`) en la tabla *Relog*.
- `docs/FUNCIONAMIENTO.md`: viñeta nueva "Reintento rápido del click de *Start*" describiendo el
  discriminador por comparación de cuadros y el cambio de método de click.
- `docs/DESARROLLO.md`: fila nueva en "Lecciones aprendidas" con síntoma/causa/qué se hace.
- `docs/CONTEXTO_IA.md`: actualizado el renglón de "Pendiente / límites" sobre el click de *Start*.

## 6. Pruebas

### 6.1 Arnés (por qué no `tools/simulate_relog.py`)

`tools/simulate_relog.py` pide una carpeta con capturas de pantalla completa
(`disc.png, server.png, chan1.png, chan4.png, pin.png, ann.png, char.png, trade.png, world.png`,
1920×1032) que **no están en el repo**. Para validar sin el juego real armé un arnés que compone
pantallas sintéticas con las plantillas `sd` sobre fondo gris, en las coordenadas del log, y corre el
**motor real** (`Relog.step()`, `detect`, `find`, `click`) sobre un "juego falso" con reloj simulado.
El arnés está en el archivo temporal `start_test.py` (fuera del repo).

Casos probados:

1. *Start* a la primera.
2. Primer *Start* perdido (el juego ignora el primer click).
3. *Start* OK + carga lenta de 1.6 s (transición animada) — para verificar que **no** reintenta de más.

### 6.2 Resultados (demora desde que aparece *Select Server* hasta que la abandona)

| Caso | Antes | Después |
|---|---|---|
| *Start* a la primera | 0.65 s | 0.65 s |
| Primer *Start* perdido | **3.75 s** | **1.65 s** |
| *Start* OK + carga lenta 1.6 s | — | **0.65 s** (sin reintento falso) |

### 6.3 Verificación de una falla intermedia (útil para la revisión)

La primera versión usaba **promedio** de la diferencia (`< 1.5`) sobre una firma 48×27. Con eso, el
caso "carga lenta" **disparaba un reintento falso** (movimiento localizado y tenue daba promedio bajo).
Se cambió a **máximo** de la diferencia sobre 96×54 con umbral `> 6`, lo que separa limpiamente
"idéntico" (máx. 0) de "en movimiento" (máx. 120 en la prueba). Es el punto más discutible del diseño:
si en el juego real una carga produce diferencias muy bajas por cuadro, el umbral `> 6` podría ser
chico; conviene revisarlo con capturas reales.

## 7. Riesgos y preguntas para Claude

1. **Umbral de movimiento `> 6` (máximo sobre 96×54).** ¿Es robusto? Una transición real podría
   empezar con un cambio mínimo y clasificarse como "idéntica" -> reintento espurio. Alternativas:
   muestrear más de un cuadro, subir el umbral, o exigir que la firma *posterior* también sea igual a
   un cuadro más para confirmar.
2. **`server_early_probe = 1.0 s`.** Muy bajo podría reintentar mientras la pantalla aún no refleja un
   click válido (el juego procesa la entrada por cuadro). ¿Conviene 1.2–1.5 s?
3. **Comparación global por máximo.** Un dip muy localizado (p. ej. el cursor que se mueve) podría
   contar como "cambio". En el arnés el cursor se estaciona (`PARK`) tras cada click, pero en vivo
   podría variar. ¿Conviene comparar solo una región (p. ej. el área del botón / cabecera)?
4. **Interacción con `MIN_REPEAT`.** El reintento rápido no toca `MIN_REPEAT` (`last_act["server"]` sí
   se actualiza); el camino rápido se controla con `server_probe_until`/`server_retry_at`. ¿Se ve
   correcto que convivan ambos?
5. **Falsos positivos.** El bloque corre **antes** de `detect()`. Si un estilo de texto nuevo no
   matchea `server_hdr` (score < 0.8) no se dispara (correcto). Si `server_hdr` matchea por error en
   otra pantalla, `find(gray, "start_btn", ...)` en la ventana y `_server_stuck` actúan como segunda
   barrera. ¿Alcanza?
6. **`in_world`.** Depende del título de la ventana; en el login el título es solo
   "Angels Online Global", así que `in_world=False` y aplica. ¿Algún caso donde el título quede "en el
   mundo" con una pantalla de servidor visible?

## 8. Estado / pendientes

- `python -m py_compile autorelog.py` -> OK.
- Sin lint/formatter configurados en el repo (no hay ruff/flake8 ni config).
- Falta validar con una relogueada real: revisar en `autorelog.log` que **no** aparezca
  `"Start no avanzó; reintento"` tras un *Start* que sí funcionó (sería un reintento falso).
- No commiteado.