# Arquitectura

AutoRelog es una aplicación de escritorio de un solo proceso (Python + tkinter) con cuatro módulos y varios hilos. No toca el proceso del juego: lo **observa** capturando su ventana y le **envía** mensajes de entrada.

## Vista general

```mermaid
flowchart LR
    subgraph APP["Proceso AutoRelog (pythonw.exe)"]
        direction TB
        GUI["gui.py<br/>Interfaz tkinter<br/>(hilo principal)"]
        ENG["autorelog.py<br/>Motor Relog<br/>(hilo worker)"]
        NET["netmon.py<br/>NetMonitor<br/>(hilo propio)"]
    EXP["expmon.py<br/>lector y estadisticas<br/>de experiencia"]
        TRY["tray.py<br/>Icono de bandeja<br/>(hilo propio)"]
    end

    GAME["Angels Online Global<br/>angel.dat"]
    INET(("Internet<br/>1.1.1.1 / 8.8.8.8 / 9.9.9.9"))
    ROUTER(("Router<br/>(puerta de enlace)"))
    TPL[("templates/*.png")]
    DATA[("datos/<br/>cortes · latencia · sesiones · juego")]
    CFG[("config.json<br/>state.json")]
    REP[("informes/<br/>HTML + CSV")]

    GUI -- "start / stop / botones" --> ENG
    ENG -- "info de estado" --> GUI
    NET -- "on_outage_end(rec)" --> GUI
    NET -- "mensajes" --> GUI
    GUI <-->|"eventos open / toggle / exit"| TRY

    ENG -- "PrintWindow (captura)" --> GAME
    ENG -- "SendMessage: mouse y teclas" --> GAME
    ENG -- "ShowWindow: restaurar / minimizar" --> GAME
    ENG -- "lee" --> TPL

    NET -- "TCP 1 prueba/s" --> INET
    NET -- "ping durante un corte" --> ROUTER
    NET -- "escribe" --> DATA
    ENG -- "caida_juego / reingreso" --> DATA
    ENG -- "captura en color cada ~10 s" --> EXP
    EXP -- "exp.csv" --> DATA
    GUI -- "ritmo y gráficos" --> EXP
    GUI -- "estadisticas y exportar" --> DATA
    GUI --> REP
    GUI -- "lee / guarda" --> CFG
    ENG -- "lee / guarda canal" --> CFG
```

## Módulos

| Módulo | Responsabilidad | No debería |
|---|---|---|
| `autorelog.py` | Encontrar la ventana, capturarla, reconocer pantallas por plantillas, decidir qué hacer y ejecutar clicks/teclas. Contiene la clase `Relog` (máquina de estados). | Conocer tkinter. Funciona también por consola (`autorelog.py --live`). |
| `gui.py` | Interfaz, ajustes, pestaña de conexión, ciclo de vida de los hilos, bandeja, instancia única. | Hacer clicks al juego directamente (siempre pasa por `Relog`). |
| `netmon.py` | Medir internet, detectar cortes, guardar datos, calcular estadísticas y generar informes. | Saber del juego, salvo registrar eventos que le pasa el motor. |
| `expmon.py` | Leer el porcentaje de experiencia de una captura y calcular ritmo, ETA, subidas de nivel y estancamiento. No sabe de ventanas ni de clicks. | Capturar la pantalla: recibe una imagen. |
| `tray.py` | Icono en los iconos ocultos, menú, globos de notificación. Solo `pywin32`. | Tener lógica de negocio: solo emite eventos (`open`, `toggle`, `exit`). |

El acoplamiento es deliberadamente unidireccional: `gui` conoce a los demás, `autorelog` solo importa `netmon` (eventos del juego) y `expmon` (lectura de experiencia), y `netmon`/`expmon`/`tray` no importan a ningún otro módulo del proyecto.

## Hilos

```mermaid
flowchart TB
    subgraph T1["Hilo principal (tkinter)"]
        A1["tick() cada 250 ms<br/>• vacia la cola de logs<br/>• atiende eventos de la bandeja<br/>• pinta el estado"]
        A2["refresh_net_header() 1 s<br/>refresh_net() 2 s<br/>refresh_client() 2 s"]
    end
    subgraph T2["Hilo worker (uno por 'Iniciar')"]
        B1["bucle: Relog.step()<br/>luego espera next_delay<br/>0.35 s mientras reloguea / 3 s en reposo"]
    end
    subgraph T3["Hilo netmon"]
        C1["ciclo de 1 s: 3 conexiones TCP en paralelo<br/>(ThreadPoolExecutor de 3)"]
        C2["hilo auxiliar: averiguar el router<br/>(PowerShell, cada 5 min)"]
    end
    subgraph T4["Hilo tray"]
        D1["PumpMessages()<br/>ventana oculta clase AutoRelogTray"]
    end
    subgraph T5["Hilos de un solo uso"]
        E1["botones 'Probar' y 'Angel Baby'"]
    end

    B1 -.-|"Relog._lock"| E1
    T3 -- "queue de logs" --> A1
    T4 -- "tray_q" --> A1
```

Reglas de sincronización:

- **`Relog._lock`**: el ciclo del worker y los botones de prueba comparten el motor; el `RLock` garantiza que solo un hilo a la vez haga clicks sobre el juego.
- **`NetMonitor.lock`**: protege el estado que lee la interfaz (`snapshot()`, `current_minute()`).
- **Cola de logs**: los hilos nunca tocan widgets. Escriben al `logging`, un `QueueHandler` encola y `tick()` (hilo principal) pinta.
- La bandeja avisa a la interfaz solo con una `queue.Queue`.

## Ciclo de un paso del motor

```mermaid
flowchart TD
    S["Relog.step()"] --> W{"¿hay ventana<br/>de angel.dat?"}
    W -- no --> W0["estado: sin ventana"]
    W -- si --> M{"¿minimizada?"}
    M -- si --> P{"¿pedido de revision<br/>o toca revision periodica?"}
    P -- si --> R["ShowWindow SHOWNOACTIVATE<br/>restored_by_us = True"]
    P -- no --> M0["estado: minimizada (no se puede ver)"]
    M -- no --> T["leer titulo:<br/>¿termina en (Servidor-N)?"]
    T --> C["capturar ventana → gris"]
    C --> D["detect(): plantillas por prioridad"]
    D --> H{"¿reconoce algo?"}
    H -- no --> I{"¿en el mundo?"}
    I -- si, AB pendiente --> AB["handle_ab(): Ctrl+R · Attack · cerrar"]
    I -- si, nada pendiente --> Q["dormir al juego · re-minimizar si lo restauramos"]
    I -- no --> L["pantalla cargando: mirar rápido"]
    H -- si --> O{"¿hay internet?<br/>¿se clickeó hace poco?"}
    O -- no --> O0["esperar"]
    O -- si --> N["throttled() → nivel de click → do()"]
```

Más detalle en [FUNCIONAMIENTO.md](FUNCIONAMIENTO.md).

## Decisiones de diseño (y por qué)

| Decisión | Motivo |
|---|---|
| **Capturar con `PrintWindow(PW_RENDERFULLCONTENT)`** en vez de capturar la pantalla | Funciona aunque otra ventana tape al juego. No funciona minimizado (por eso existe el "peek"). |
| **Reconocer por plantillas** (`cv2.matchTemplate`) y no por posiciones fijas | Las pantallas se identifican por su aspecto; los botones se ubican respecto del marcador, así que sobreviven a que el diálogo cambie de lugar. |
| **Enviar mensajes (`SendMessage`)** y no `SendInput` | No mueve el mouse real ni requiere foco. |
| **"Despertar" al juego** antes de cada entrada (`WM_ACTIVATEAPP`, `WM_NCACTIVATE`, `WM_ACTIVATE`, `WM_SETFOCUS`) | El cliente ignora mouse y teclado si cree que no está activo. Es lo que permite el modo segundo plano. |
| **Dejar al juego "despierto" durante toda una secuencia** y dormirlo recién al terminar | Dormirlo justo después de cada entrada hacía que descartara la tecla (la lee en su siguiente cuadro). |
| **Pulsaciones largas** (tecla 0.3 s, click 0.1 s ampliable a 0.4 s) | El juego lee la entrada por cuadro; una pulsación corta puede caer entre dos cuadros y perderse. |
| **Estacionar el cursor** del juego en `PARK = (900, 30)` tras cada click | El juego dibuja su cursor donde le mandamos el último movimiento: tapaba las plantillas y dejaba botones en estado *hover*, que bajaba la coincidencia. |
| **Click largo desde el primer intento** en carteles y login, y escalar si un paso se repite | El click corto nunca registraba ahí (el registro lo mostró en cada relog). Si el segundo plano falla igualmente, ese click usa el mouse real sin degradar el resto. |
| **Verificar visualmente la fila del canal** antes de aceptar | Un click perdido dejaba elegido el canal por defecto (el 1, a menudo lleno) y el relog terminaba en el canal equivocado. |
| **Monitor de internet independiente del juego** | Debe medir aunque la vigilancia esté detenida y servir como evidencia por sí solo. |
| **Tres servidores y 2 fallas seguidas** para declarar un corte | Evita falsos cortes por un servidor caído o una pérdida de un solo paquete. |
| **Escritura atómica** de `config.json` y `estado.json`, más copia `.bak` | Un corte de luz a mitad de escritura dejaba un JSON roto y el programa no arrancaba. |
| **Instancia única con mutex + ventana oculta de bandeja** | Dos instancias medirían a la vez sobre los mismos archivos. |

## Archivos en tiempo de ejecución

```mermaid
flowchart LR
    subgraph repo["En el repositorio"]
        PY["*.py"]
        T["templates/"]
        EX["config.example.json"]
    end
    subgraph local["Solo en tu PC (ignorados por git)"]
        C["config.json (+ .bak)"]
        S["state.json"]
        D["datos/"]
        I["informes/"]
        L["autorelog.log (rota 1 MB × 3)"]
        IC["icon_ok.ico · icon_down.ico (se generan solos)"]
    end
    PY --> local
    EX -. "copiar" .-> C
```

Formatos de los archivos de `datos/` en [DATOS.md](DATOS.md); todas las opciones de `config.json` en [CONFIGURACION.md](CONFIGURACION.md).
