# Home Assistant Smart Home · Quick Start

> **In one line**: you already run a Home Assistant at home; this plugin translates phrases like "turn on the living room light" into its service calls.

---

## 1. Prepare: address and token

### Find your Home Assistant address

Open the HA web UI you normally use and look at the address bar:

- `http://homeassistant.local:8123`
- `http://192.168.1.50:8123`
- or whatever domain your reverse proxy / tunnel exposes

Strip the trailing page path so only `scheme://host:port` remains. For example `http://192.168.1.50:8123/lovelace/0` → `http://192.168.1.50:8123`. (The plugin also strips common front-end paths automatically, but a clean value is safer.)

### Create a long-lived access token

1. In the HA UI click your **avatar** (bottom-left on newer versions)
2. Open the **Security** tab
3. Scroll to **Long-lived access tokens**
4. Click **Create token**, name it anything (e.g. `N.E.K.O`)
5. **Copy it immediately** — the `eyJ0eXAiOiJKV1Qi...` string is shown only once

> This token carries your account's permissions. The plugin stores it at `plugins/homeassistant/data/credential.json` (atomic write, tightened permissions) and only echoes the last 4 characters. **Never** paste it into a chat window or screenshot.

---

## 2. Connect

1. Open N.E.K.O → plugin panel → **Home Assistant Smart Home**
2. Enter the address and token
3. Click **Test connection** — you should see the HA version, entity count, and whether the user is an admin
4. Click **Save**

Then click **Refresh devices** once so the plugin pulls the entity list (cached in memory for 60 seconds afterwards).

---

## 3. Control with your voice

Just talk to your AI companion:

| Goal | Say |
| --- | --- |
| Power | `turn on the living room light` / `turn off the study light` |
| Brightness | `set the living room light to 40%` / `make the desk lamp brighter` |
| Color temp | `bedroom light 3000K` / `make the ceiling light warmer` |
| Climate | `set the AC to 26 degrees` / `living room AC cooling` / `AC fan speed auto` |
| Cover | `open the curtains` / `open the curtains halfway` |
| Media | `speaker volume 30` / `mute the TV` / `pause` |
| Scene | `run the coming home scene` / `trigger away mode` |

Fractions and vague values work too: `halfway` = 50 %, `mostly` = 70 %, `brightest` / `dimmest` / `lowest` / `highest`, `a bit louder`, `volume down by 10`, decimals like `26.5 degrees`.

---

## 4. Too many devices?

With three lights at home, `turn on the light` triggers disambiguation:

```
Found 3 devices matching "light":
1. 🟢 Living Room Ceiling Light
2. 🔴 Bedroom Ceiling Light
3. 🟢 Hallway Downlight
Please specify with "area + device", e.g. "bedroom ceiling light"
```

Two ways out:

- **Add the area name**: `turn on the bedroom ceiling light` (area names come from HA's Areas settings, not ones you invent)
- **Set an alias**: in the panel, give an entity the name you actually use — e.g. rename `light.yeelight_ceiling_2` to "main light"

---

## 5. Safety confirmation

Garage doors, gates and locks are never executed directly. The plugin returns an 8-character confirmation code:

```
> open the garage door
< About to run "open the garage door" — this has safety implications. Code: a3f9c1d2
  Reply with that code within 120 seconds to continue.

> a3f9c1d2
< Done: open the garage door
```

The code lives in process memory only, is single-use, only the newest one is kept, and expires after 120 s. A host restart invalidates it.

---

## 6. No Home Assistant yet? (try-out mode)

**This plugin does not include or install Home Assistant.** That is deliberate: HA is a standalone service, device discovery must happen on the network where HA lives, and its size plus release cadence do not belong inside a plugin.

The quickest way to try it is Docker on an always-on machine (NAS, mini PC, or even your local box):

```bash
docker run -d \
  --name homeassistant \
  --restart=unless-stopped \
  --network=host \
  -v /path/to/ha-config:/config \
  ghcr.io/home-assistant/home-assistant:stable
```

Then visit `http://<that-machine-ip>:8123`, create an account, and issue a token as in section 1. No real devices needed — HA lets you add virtual entities (`input_boolean`, template lights), which is enough to exercise the plugin.

> ⚠️ Use `--network=host` so HA can auto-discover LAN devices (especially anything relying on broadcast, like Mijia/Tuya). Bridged networking will miss them.

---

## 7. FAQ

**Q: It says "not configured".**
A: No connection saved yet. Fill in the address and token, then click Save.

**Q: Test fails with 401 / 403.**
A: Wrong or expired token. Recreate it on HA's Security page. 403 usually means the user lacks permissions.

**Q: Test fails, cannot reach the host.**
A: The plugin runs on the N.E.K.O machine and must reach HA **from there**. Try `ping` or open the URL in a browser on that machine first. If `homeassistant.local` does not resolve, use the IP.

**Q: It controls devices but never knows area names.**
A: The WebSocket registries are unavailable (`get_status` shows the degradation reason). Most often the token's user is not an admin — recreate the token with an admin account. Matching still works by entity name while degraded.

**Q: Newly added devices are missing.**
A: Click "Refresh devices" in the panel. The entity snapshot is cached for 60 seconds.

**Q: Vacuum / fan / lock support?**
A: Currently focused on six core domains: light, switch, climate, cover, media_player, scene. Anything else is explicitly reported as unsupported rather than silently pretended.

---
*Doc version: 2026-11-18*
