# Configuración

La configuración vive en `config.json`, junto al código. No viene en el repositorio: se crea al guardar desde la pestaña **Ajustes** (o copiando `config.example.json`). Lo que falte toma el valor por defecto de `DEFAULTS` en `autorelog.py`.

- Se escribe de forma atómica y se conserva la versión anterior válida en `config.json.bak`.
- Si `config.json` está dañado, se lee `config.json.bak`; si tampoco sirve, se usan los valores por defecto.
- La interfaz guarda **toda** su configuración en memoria al cambiar algo. Si editás el archivo a mano, cerrá antes la aplicación, o tus cambios se pisarán.

## Opciones

### Relog

| Clave | Por defecto | Descripción |
|---|---|---|
| `dry_run` | `true` | Modo **ensayo**: detecta y anota qué haría, sin clickear ni restaurar el juego. Se cambia con el interruptor `ensayo / live`. |
| `input` | `"background"` | `background`: mensajes de ventana, sin mover tu mouse. `foreground`: mouse real (trae el juego al frente en cada click). |
| `fallback_real_mouse` | `true` | Si un paso se repite sin avanzar, ese click escala hasta el mouse real. |
| `channel_fixed` | `0` | `0` = volver al último canal recordado; `1`–`5` = canal fijo. |
| `default_channel` | `4` | Canal a usar si todavía no se recordó ninguno. |
| `dismiss_trade_pin` | `true` | Cerrar con ✘ el aviso "¿Quieres poner un Trade PIN?". |
| `max_attempts` | `8` | Veces que se acciona una misma pantalla en 60 s antes de pausar 60 s. |
| `threshold` | `0.8` | Umbral de coincidencia de las plantillas (0–1). |
| `server_early_probe` | `1.0` | Segundos tras clickear *Start* antes de comprobar si la pantalla cambió. Si sigue idéntica, el click se perdió y se reintenta sin esperar `MIN_REPEAT`. Para reintentos subsecuentes escala al método de click. |
| `server_early_retry` | `false` | Reintentar *Start* apenas la pantalla sigue idéntica (**experimental**: si el servidor tarda con la pantalla quieta, hace clicks de más). Con `false` solo **mide** y deja en el registro la línea `Start: diferencia máxima…` para calibrar. |

### Tiempos

| Clave | Por defecto | Descripción |
|---|---|---|
| `poll_seconds` | `3` | Cada cuánto mira el juego en reposo (dentro del mundo). |
| `fast_poll` | `0.35` | Cada cuánto mira mientras reloguea. |
| `action_cooldown` | `0.3` | Pausa mínima tras cada acción. |
| `ab_delay` | `2` | Segundos en el mundo antes de activar el Angel Baby. |

### Angel Baby

| Clave | Por defecto | Descripción |
|---|---|---|
| `angel_baby_after_relog` | `true` | Tras reloguear: `Ctrl+R` y tildar *Attack*. |
| `ab_close_panel` | `true` | Cerrar el panel al terminar. |

### Capturas

| Clave | Por defecto | Descripción |
|---|---|---|
| `capture_unknown` | `true` | Guardar en `capturas/` las pantallas del login no reconocidas, para armar plantillas de un estilo nuevo. |

### Experiencia

| Clave | Por defecto | Descripción |
|---|---|---|
| `exp_tracking` | `true` | Leer el % de experiencia del HUD cada ~10 s (con el juego visible y en el mundo). |
| `exp_stall_min` | `10` | Avisar si pasan estos minutos sin ganar experiencia (`0` = nunca). |

### Juego minimizado

| Clave | Por defecto | Descripción |
|---|---|---|
| `restore_minimized` | `true` | Restaurarlo (sin foco) cuando haga falta revisarlo y volver a minimizarlo. |
| `peek_min_outage` | `3` | Segundos de corte de internet a partir de los cuales se revisa el juego al volver la conexión. |
| `peek_during_outage_s` | `45` | Si el internet lleva tantos segundos caído y el juego está minimizado, restaurarlo ya para ver el cartel y reloguear apenas vuelva (`0` = no). |
| `peek_dwell_s` | `45` | Tras un corte, cuánto se queda restaurado mirando antes de volver a minimizarlo. |
| `peek_every_min` | `10` | Revisión periódica si sigue minimizado (`0` = nunca). |

### Aplicación

| Clave | Por defecto | Descripción |
|---|---|---|
| `autostart` | `true` | Empezar a vigilar al abrir la aplicación. |
| `minimize_to_tray` | `true` | La ✕ oculta la ventana en la bandeja en vez de cerrar. |
| `tray_notifications` | `true` | Globos de notificación con la ventana oculta. |
| `start_in_tray` | `false` | Arrancar sin mostrar la ventana. |
| `isp` | `""` | Nombre del proveedor que aparece en el informe de conexión. |

### Identificación del juego

| Clave | Por defecto | Descripción |
|---|---|---|
| `process` | `"angel.dat"` | Nombre del proceso del cliente. |
| `title_prefix` | `"Angels Online Global"` | Con qué empieza el título de su ventana. |

## `state.json`

Guarda lo que el programa recuerda entre sesiones (hoy, el último servidor y canal):

```json
{"channel": 4, "server": "Athena"}
```

Se actualiza solo leyendo el título de la ventana del juego. No hace falta tocarlo.
