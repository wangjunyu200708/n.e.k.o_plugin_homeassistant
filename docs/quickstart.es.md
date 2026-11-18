# Home Assistant · Casa inteligente · Inicio rápido

> **En una frase**: ya tienes un Home Assistant funcionando en casa; este plugin traduce frases como «enciende la luz del salón» en llamadas a sus services.

---

## 1. Preparación: dirección y token

### Encuentra la dirección de tu Home Assistant

Abre la interfaz web de HA que uses normalmente y mira la barra de direcciones:

- `http://homeassistant.local:8123`
- `http://192.168.1.50:8123`
- o el dominio que exponga tu proxy inverso / túnel

Elimina la **ruta de página** final y deja solo `esquema://host:puerto`. Por ejemplo `http://192.168.1.50:8123/lovelace/0` → `http://192.168.1.50:8123` (el plugin también recorta las rutas front-end habituales, pero escribirlo limpio es más seguro).

### Crea un token de acceso de larga duración

1. En HA pulsa tu **avatar** (abajo a la izquierda en versiones recientes)
2. Abre la pestaña **«Seguridad»**
3. Baja hasta **«Tokens de acceso de larga duración»**
4. Pulsa **«Crear token»**, pon cualquier nombre (p. ej. `N.E.K.O`)
5. **Cópialo de inmediato** — la cadena `eyJ0eXAiOiJKV1Qi...` solo se muestra una vez

> Este token equivale a los permisos de tu cuenta. El plugin lo guarda **cifrado**: DPAPI en Windows (vinculado a tu usuario y máquina), el llavero del sistema en el resto. Si ninguno está disponible, se mantiene solo en memoria y hay que volver a introducirlo tras reiniciar; **nunca** se escribe en texto plano. Solo se muestran los últimos 4 caracteres. **No** lo pegues en un chat ni en una captura.

---

## 2. Conectar

1. Abre N.E.K.O → panel de plugins → **Home Assistant · Casa inteligente**
2. Confirma la dirección — viene rellenada con `http://127.0.0.1:8123`; cámbiala por la IP del equipo donde corre HA si no es este. Después pega el token
3. Pulsa **«Probar conexión»** — verás la versión de HA, el número de entidades y si el usuario es administrador
4. Pulsa **«Guardar»**

Después pulsa una vez **«Actualizar dispositivos»** para que el plugin descargue la lista de entidades (luego hay caché en memoria durante 60 segundos).

---

## 3. Controlar con la voz

Simplemente habla con tu compañero AI:

| Objetivo | Di |
| --- | --- |
| Encender/apagar | `enciende la luz del salón` / `apaga la luz del estudio` |
| Brillo | `pon la luz del salón al 40%` / `sube el brillo de la lámpara` |
| Temperatura de color | `luz del dormitorio 3000K` / `pon el techo más cálido` |
| Climatización | `pon el aire a 26 grados` / `aire del salón en frío` / `velocidad del aire automática` |
| Persianas | `abre las cortinas` / `abre las cortinas a la mitad` |
| Multimedia | `volumen del altavoz 30` / `silencia la tele` / `pausa` |
| Escenas | `ejecuta la escena llegar a casa` / `activa el modo ausente` |

**La comprensión la hace el modelo de chat de N.E.K.O.** El plugin no analiza el texto. Por eso «a medias» o «un poco más» también funcionan: el modelo lo convierte en `brightness: 50` o te pregunta primero un valor concreto. Cuanto más vaga sea la petición, más probable es que pregunte; y eso es mejor que actuar sobre el dispositivo equivocado.

---

## 4. ¿Demasiados dispositivos?

Con tres luces en casa, `enciende la luz` devuelve «coinciden varias» y el asistente pregunta cuál:

```
Coinciden 3 dispositivos «luz»:
1. Salón Lámpara de techo (light.living_room_ceiling)
2. Dormitorio Lámpara de techo (light.bedroom_ceiling)
3. Recibidor Foco empotrado (light.hallway_downlight)
```

**El plugin nunca elige uno por ti** — prefiere preguntar una vez antes que equivocarse. Tres salidas:

- **Sé más concreto**: `enciende la lámpara del dormitorio` (los nombres de zona vienen de los Ajustes de áreas de HA, no los inventas tú)
- **Usa un alias**: en el panel, ponle a la entidad el nombre que realmente usas — por ejemplo, renombra `light.yeelight_ceiling_2` a «luz principal»
- **Nombra una zona**: `apaga todas las luces del despacho` — el asistente lista primero las luces de esa zona y las envía una a una tras confirmar

---

## 5. Confirmación de seguridad

El plugin gestiona los permisos en tres niveles de riesgo, y **los dispositivos de alto riesgo no son controlables en absoluto por defecto**:

| Nivel | Dispositivos | Por defecto |
| --- | --- | --- |
| Bajo | luces, ventiladores, reproductores, escenas, cortinas normales | permitido |
| Medio | interruptores/enchufes, clima | permitido (marcado en el panel) |
| Alto | cerraduras, paneles de alarma, válvulas, calentadores, puertas de garaje | **bloqueado** |

Los dispositivos de alto riesgo solo se habilitan **uno a uno** en el panel; no existe «seleccionar todas las cerraduras». Aun así, cada acción requiere una segunda confirmación: la autorización decide *si puedes* controlarlo, no *si te pregunta antes*:

```
> abre la puerta principal
< Acción de alto riesgo, no ejecutada. Voy a ejecutar «unlock» en «Cerradura principal». Código: a3f9c1d2 (válido 120 s)

> a3f9c1d2
< Hecho: lock.unlock — la relectura confirma «unlocked»
```

El código es de un solo uso, está ligado a «dispositivos + acción + parámetros», deja de valer si cambias el objetivo y caduca a los 120 s. Las direcciones que hacen el mundo **más seguro** — apagar, cerrar con llave, armar — nunca se bloquean.

---

## 6. ¿Aún no tienes Home Assistant? (modo prueba)

**Este plugin no incluye ni instala Home Assistant.** Es deliberado: HA es un servicio independiente, el descubrimiento de dispositivos debe ocurrir en su red, y su tamaño y ritmo de actualizaciones no encajan dentro de un plugin.

La vía más rápida para probarlo es Docker en una máquina siempre encendida (NAS, mini PC o incluso tu equipo local):

```bash
docker run -d \
  --name homeassistant \
  --restart=unless-stopped \
  --network=host \
  -v /path/to/ha-config:/config \
  ghcr.io/home-assistant/home-assistant:stable
```

Luego entra en `http://<IP-de-esa-máquina>:8123`, crea la cuenta y emite un token como en la sección 1. No hacen falta dispositivos reales: HA permite añadir entidades virtuales (`input_boolean`, luces de plantilla), suficiente para validar el plugin.

> ⚠️ Usa `--network=host` para que HA descubra dispositivos de la LAN (sobre todo lo que depende de broadcast, como Mijia/Tuya). Con red bridge se perderán.

---

## 7. Preguntas frecuentes

**P: Dice «no configurado».**
R: Aún no hay conexión guardada. Introduce la dirección y el token, y pulsa Guardar.

**P: La prueba falla con 401 / 403.**
R: Token incorrecto o revocado. Crea uno nuevo en la página «Seguridad» de HA. Un 403 suele indicar permisos insuficientes del usuario.

**P: La prueba no llega al host.**
R: El plugin se ejecuta en la máquina de N.E.K.O y debe alcanzar HA **desde ahí**. Prueba primero `ping` o abre la URL en el navegador de esa máquina. Si `homeassistant.local` no resuelve, usa la IP.

**P: Controla dispositivos pero nunca reconoce nombres de zona.**
R: Los registros WebSocket no están disponibles (`get_status` muestra el motivo). Lo más común es que el usuario del token no sea administrador: crea el token con una cuenta admin. En modo degradado el emparejamiento sigue funcionando por nombre de entidad.

**P: No aparecen los dispositivos nuevos.**
R: Pulsa «Actualizar dispositivos» en el panel. La instantánea de entidades se cachea 60 segundos.

**Q: ¿Se admiten robots aspirador / humidificadores / cerraduras?**
A: Las cerraduras sí, pero son de alto riesgo: habilítalas una a una en el panel y cada acción pasa igualmente por una segunda confirmación. También se admiten ventiladores, válvulas, calentadores, paneles de alarma y scripts. Los robots aspirador, humidificadores y cámaras aún no se pueden controlar: el plugin lo dice claramente en vez de fingir. Los sensores no se controlan, pero **sí se consultan**.

---
*Versión del documento: 2026-11-18*
