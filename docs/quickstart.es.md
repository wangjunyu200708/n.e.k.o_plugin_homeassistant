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

> Este token equivale a los permisos de tu cuenta. El plugin lo guarda en `plugins/homeassistant/data/credential.json` (escritura atómica y permisos restringidos) y solo muestra los últimos 4 caracteres. **No** lo pegues en un chat ni en una captura.

---

## 2. Conectar

1. Abre N.E.K.O → panel de plugins → **Home Assistant · Casa inteligente**
2. Introduce la dirección y el token
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

También entiende valores aproximados: `a la mitad` = 50 %, `casi todo` = 70 %, `lo más brillante` / `lo más tenue` / `mínimo` / `máximo`, `un poco más`, `baja 10`, decimales como `26.5 grados`.

---

## 4. ¿Demasiados dispositivos?

Con tres luces en casa, `enciende la luz` provoca una desambiguación:

```
Encontrados 3 dispositivos para «luz»:
1. 🟢 Salón Lámpara de techo
2. 🔴 Dormitorio Lámpara de techo
3. 🟢 Recibidor Downlight
Especifica con «zona + dispositivo», por ejemplo «dormitorio lámpara de techo»
```

Dos soluciones:

- **Añade la zona**: `enciende la lámpara de techo del dormitorio` (las zonas vienen de los ajustes de «Zonas» de HA, no las inventes)
- **Pon un alias**: en el panel, dale a `light.yeelight_ceiling_2` el nombre que realmente usas, por ejemplo «luz principal»

---

## 5. Confirmación de seguridad

Puertas de garaje, cancels y cerraduras **nunca se ejecutan directamente**. El plugin devuelve un código de confirmación de 8 caracteres:

```
> abre la puerta del garaje
< Voy a ejecutar «abre la puerta del garaje». Tiene implicaciones de seguridad. Código: a3f9c1d2
  Responde con ese código en 120 segundos para continuar.

> a3f9c1d2
< Hecho: abre la puerta del garaje
```

El código vive solo en memoria del proceso, es **de un solo uso**, se conserva únicamente el más reciente y caduca a los 120 s. Reiniciar el host lo invalida.

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

**P: ¿Aspirador robótico / ventilador / cerradura?**
R: Ahora mismo el foco está en seis domains básicos (light, switch, climate, cover, media_player, scene). Para el resto el plugin avisa claramente de que no está soportado en vez de fingir éxito.

---
*Versión del documento: 2026-11-18*
