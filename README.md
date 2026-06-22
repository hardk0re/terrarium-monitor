# 🦎 Terrarium Monitor

A Raspberry Pi-based enclosure monitoring and automation system, built for my daughter's Crested Gecko. Yes, it's total overkill. But it works great.

---

## The Story

I looked at a dozen of these projects online and they were all either too complicated, half-finished, or required cloud accounts and subscriptions. I just wanted something that would:

- Show me temperature and humidity at a glance
- Turn the lights on and off on a schedule
- Kick on the mister if it gets too dry
- Run a fan if it gets too hot
- Let me check in on the enclosure remotely
- Keep a log of feedings and care

So I built it myself. It runs entirely on the local network — no cloud, no subscriptions, no accounts required (except an optional free OpenWeatherMap API key for outdoor weather).

---

## Features

- **Dual SHT31-D sensor monitoring** — temperature and humidity from up to N sensors (expandable via config), with optional per-sensor calibration offsets for chip-to-chip variance
- **ST7789 2" display** — sensor data by default; pre-empts to a gecko-mood page when the gecko isn't happy, and flashes a confirmation screen when a physical button is pressed
- **Day/night display brightness** — PWM-controlled backlight with a separate `night_brightness` and configurable `night_start` / `night_end` window
- **Colour-coded readings** — green = good range, blue = too cold/dry, red = too hot/humid, yellow = sensor error
- **Misting control** — 5V relay triggers automatically when humidity drops below threshold, with configurable runtime, cooldown delay, and 24-hour run cap
- **Fan control** — separate 5V relay for high/low temperature, same configurable runtime and cooldown system; threshold unit (`F`/`C`) follows the display unit or can be set per section
- **TAPO/KASA smart plug lighting** — local API control, no cloud required, fully scheduled (on/off time per plug); shutdown leaves plugs in whatever state they were in
- **TAPO camera** — ONVIF snapshot every 30 seconds, live image in web dashboard, optional timelapse viewer with scrub bar + playback controls and configurable retention
- **Outdoor weather** — OpenWeatherMap integration with current conditions card, 24h history chart, and feels-like / wind
- **Gecko mood widget** — color-changing gecko (green/yellow/red) on the dashboard derived from temperature, humidity, and check-in recency; tunable thresholds in `[gecko]`. Mood transitions are logged and Pushover-notified.
- **Pushover notifications** — per-category toggles (startup/shutdown/care/feeding/relay/error/etc.), louder sounds for errors and upset-gecko transitions, plus a one-click **Test Pushover** button on the config page
- **Care / feeding check-in reminder** — dashboard banner + Pushover when no care or feeding log has been recorded in `care_reminder_hours`
- **Sensor failure alerts** — if a sensor goes silent for `sensor_failure_alert_minutes`, an `error` event is raised (and Pushover-notified)
- **24h sensor averages widget** — average temp + half-moon humidity gauge per sensor, colour-banded with the same thresholds as the OLED
- **System stats widget** — CPU temperature, CPU %, RAM %, free disk, and uptime, with colour thresholds
- **Web dashboard** — live sensor data, charts, relay overrides, light controls, camera snapshot, feeding/care log, config editor
- **Event log viewer** — `/logs` page with category pills (care, feeding, gecko, relay, error, …), time-range filter, and full-text search
- **Feeding log** — one-tap buttons for configurable food items (crickets, waxworms, paste, etc.)
- **Care log** — one-tap buttons for configurable care activities (cleaning, misting, substrate change, vet visit, etc.)
- **Physical buttons** — up to three optional momentary push-buttons wired to GPIO pins, each with its own configurable log category (`care`, `feeding`, etc.) and message — handy when your hands are wet from misting and the web UI isn't convenient (one press → one log entry → display flash → Pushover if that category is enabled)
- **SQLite logging** — all sensor readings, weather, relay events, and care/feeding entries stored locally with configurable retention (default 31 days); web buttons to clear sensor data or the event log
- **Config editor** — edit any config value from the web UI without touching the Pi, plus a **Restart** button that asks systemd to bounce the service
- **Config reload** — most changes apply immediately without a restart
- **Systemd service** — auto-starts on boot, restarts on crash

---

## Hardware

| Component | Notes |
|-----------|-------|
| Raspberry Pi 3B+ or better | CM4 with eMMC recommended for permanent installs |
| Waveshare 2" ST7789 IPS LCD | 240×320, SPI interface |
| SHT31-D sensor module(s) | I²C, supports multiple (0x44 / 0x45) |
| 5V relay module(s) | Active-LOW, for mister and fan |
| TP-Link Tapo smart plugs | P100/P105/P110, local API, no auth required |
| TP-Link Tapo camera | ONVIF enabled, local network |
| MicroSD card | High-endurance recommended (Samsung or SanDisk) |

---

## Wiring
Usefull RPI3 Pin-Out:
<img width="1006" height="600" alt="image" src="https://github.com/user-attachments/assets/08e5c826-93c3-4873-9830-3ca978be71a0" />


### SHT31-D Sensors (I²C)
```
Sensor 1 (0x44) → ADDR pin to GND or OPEN
Sensor 2 (0x45) → ADDR pin to 3.3V

Both sensors:
  VCC → Pin 1  (3.3V)
  GND → Pin 6  (GND)
  SDA → Pin 3  (GPIO2)
  SCL → Pin 5  (GPIO3)
```

### Relay – Mister (HOME ASSISTANT)

### Relay – Fan (GPIO 27)
```
IN  → Pin 13 (GPIO27)
VCC → Pin 4  (5V)
GND → Pin 14 (GND)
```

### Care/Feeding Buttons (up to 3, optional)
Momentary push-buttons. The internal pull-up keeps each line HIGH; pressing
shorts it to GND. No external resistors needed. Each button is independent —
enable only the ones you've wired in `config.ini` under `[care_button_1]`,
`[care_button_2]`, `[care_button_3]`.

These default pins are deliberately on the bottom-right corner of the header
so they don't collide with the display (which uses GPIO 24/25) and share a
single nearby GND:

```
Button 1: GPIO13  (Pin 33) ─┐
Button 2: GPIO19  (Pin 35) ─┼──── other leg → GND on Pin 39
Button 3: GPIO26  (Pin 37) ─┘
```

Each button section sets its own `category` (e.g. `care`, `feeding`) and
`message` (e.g. `Cleaning`, `Cricket`) — when pressed, that text is logged
under that category, shows up in the event log, and fires a Pushover
notification if `notify_<category>` is enabled in `[pushover]`.

### Waveshare 2" ST7789 Display
```
VCC → Pin 17 (3.3V)
GND → Pin 20 (GND)
DIN → Pin 19 (GPIO10 / MOSI)
CLK → Pin 23 (GPIO11 / SCLK)
CS  → Pin 24 (GPIO8  / CE0)
DC  → Pin 18 (GPIO24)
RST → Pin 22 (GPIO25)
BL  → Pin 12 (GPIO18 / PWM0)
```

---

## Software Setup

### 1. Enable interfaces
```bash
sudo raspi-config
# Interface Options → I2C → Enable
# Interface Options → SPI → Enable
sudo reboot
```

### 2. Copy project files
```bash
mkdir -p /home/pi/terrarium
cd /home/pi/terrarium
# copy all project files here
mkdir -p data
```

### 3. Create virtual environment and install dependencies
```bash
python3 -m venv venv
source venv/bin/activate

pip install \
  adafruit-blinka \
  adafruit-circuitpython-sht31d \
  RPi.GPIO \
  spidev \
  Pillow \
  flask \
  PyP100 \
  requests \
  opencv-python-headless
```

### 4. Configure
```bash
cp sample_config.ini config.ini
vi config.ini
```

Key things to set:
- `[tapo_plug_1]` / `[tapo_plug_2]` → IP addresses of your smart plugs
- `[camera]` → IP, ONVIF port, username, password
- `[weather]` → your OpenWeatherMap API key, latitude, longitude
- `[display]` → `temp_unit = F` or `C`
- `[mister]` / `[fan]` → thresholds and runtimes for your setup
- `[web]` → `site_title`, auth settings

### 5. Test run
```bash
source venv/bin/activate
python main.py
```

Open `http://<pi-ip>:8080` in your browser.

### 6. Install as a systemd service
```bash
sudo cp terrarium.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable terrarium
sudo systemctl start terrarium

# Check logs
sudo journalctl -u terrarium -f
```

---

## File Structure

```
terrarium/
├── config.ini                  ← all settings
├── main.py                     ← entry point
├── sensor_manager.py           ← SHT31 sensor reading
├── data_logger.py              ← SQLite storage
├── relay_controller.py         ← GPIO relay control
├── tapo_controller.py          ← smart plug control
├── display_manager.py          ← ST7789 display driver
├── camera_manager.py           ← TAPO ONVIF camera
├── weather_manager.py          ← OpenWeatherMap
├── web_dashboard.py            ← Flask web UI
├── care_button.py              ← GPIO push-buttons (up to 3) → log entries
├── gecko_mood.py               ← shared mood scoring (dashboard + OLED)
├── pushover_notifier.py        ← Pushover push notifications
├── terrarium.service           ← systemd unit
└── stls/
    ├── LCD-CASE.stl            ← Front of LCD Case
    ├── LCD-REARCOVER.stl       ← Rear Cover
    ├── LCD-RETENTION.stl       ← Retention bar for LCD
    ├── LCD-ButtonRetention.stl ← Retention plates for standard project buttons
    ├── SHT31_BOTTOM.stl        ← SHT31 Case - Bottom
    ├── SHT31_TOP.stl           ← SHT31 Case - Top
└── data/
    ├── terrarium.db        ← SQLite database (auto-created)
    ├── terrarium.log       ← log file (auto-created)
    ├── snapshot.jpg        ← latest camera snapshot
    └── timelapse/          ← timelapse frames (if enabled)
```

---

## Web Dashboard

| URL | Description |
|-----|-------------|
| `http://<pi-ip>:8080/` | Live dashboard |
| `http://<pi-ip>:8080/logs` | Filterable event log viewer |
| `http://<pi-ip>:8080/timelapse` | Timelapse viewer with playback / scrubber |
| `http://<pi-ip>:8080/config` | Config editor (password protected) |
| `http://<pi-ip>:8080/api/status` | JSON status snapshot |
| `http://<pi-ip>:8080/api/history?hours=24` | Sensor history |
| `http://<pi-ip>:8080/api/sensors/averages?hours=24` | Per-sensor 24h averages |
| `http://<pi-ip>:8080/api/weather/history` | Weather history |
| `http://<pi-ip>:8080/api/gecko/status` | Current gecko mood + per-factor reasons |
| `http://<pi-ip>:8080/api/care/status` | Whether a care/feeding check-in is overdue |
| `http://<pi-ip>:8080/api/system/stats` | CPU temp / CPU % / RAM / free disk / uptime |
| `http://<pi-ip>:8080/api/logs?hours=24&categories=care,feeding&q=cricket` | Filtered event log |
| `POST /api/relay/Mister/on` | Force mister on |
| `POST /api/relay/Fan/off` | Force fan off |
| `POST /api/light/UVB%20Light/on` | Force plug on |
| `POST /api/pushover/test` | Send a test Pushover notification |
| `POST /api/sensors/clear` | Wipe all indoor sensor history |
| `POST /api/log/clear` | Wipe the event + relay log |

---

## Control Logic

```
Every poll_interval_seconds (default 30s):

  MISTER:
    if avg_humidity < humidity_threshold_low
    AND not running AND not in cooldown
    AND 24h run count < mister_max_runs_per_24h:
      → ON for mister_runtime_minutes
      → cooldown for mister_monitor_delay_minutes
      → re-evaluate

  FAN:
    if avg_temp > temp_threshold_high  (too hot)
    OR avg_temp < temp_threshold_low   (too cold)
    AND not running AND not in cooldown
    AND 24h run count < fan_max_runs_per_24h:
      → ON for fan_runtime_minutes
      → cooldown for fan_monitor_delay_minutes
      → re-evaluate

  LIGHTS:
    Background thread checks every 60s:
    if current time between on_time and off_time → ON
    else → OFF
```

---

## Adding More Sensors

Add a new `[sensor_N]` section to `config.ini` — no code changes needed:

```ini
[sensor_3]
enabled = true
name = Basking Spot
i2c_address = 0x44
i2c_bus = 3
```

> **Note:** Only two I²C addresses are available on a single bus (0x44 and 0x45). For more than two sensors on the same bus, use a TCA9548A I²C multiplexer.


## Security

> ⚠️ **Do not expose this dashboard directly to the public internet.** It's
> designed for use on a trusted LAN only. The dashboard is wide open by
> default, multiple destructive endpoints (`/api/relay/*`, `/api/shutdown`,
> `/api/sensors/clear`, `/api/log/clear`, the config editor) require no auth
> unless you flip `auth_enabled = true`, and there's no CSRF protection or
> HTTPS in front of Flask.

**Use it on your home LAN.** That's the whole threat model the project was
built for. Family members poking buttons is fine — exposing port 8080 on
your router is not.

**If you genuinely need to reach it while away from home**, use a VPN

**Even on your LAN**, a few cheap hardening steps are worth doing:

1. Set `auth_enabled = true` in `[web]` and change the default `password = terrarium`.
2. Change `app.secret_key` in `web_dashboard.py` to a random value
   (`python3 -c "import secrets; print(secrets.token_hex(32))"`). Without
   this, anyone who has seen the public repo can forge a logged-in session
   cookie.
3. Treat **anything you've ever put in `config.ini` while it was in a public
   git repo as compromised** — rotate camera, Pushover, OpenWeather, and any
   smart-plug credentials that have been pushed publicly.
4. Add a `.gitignore` for `config.ini` and `data/` so secrets and your
   sensor history don't end up in commits going forward.

## Notes

- The TAPO camera requires ONVIF to be enabled in the Tapo app under **Advanced Settings → ONVIF**
- Smart plugs on newer firmware may reject unauthenticated local control by default. In the Kasa app, open each device → **Advanced Settings → Third-party compatibility** (sometimes called *Allow third party control*) and enable it. With that toggle on, `PyP100`'s empty-credential path works again on the LAN.
- The web config editor saves immediately but some changes (GPIO pins, I²C addresses, web port) require a restart to take effect
- All data is stored locally in SQLite — nothing leaves your network
- Timelapse frames accumulate quickly; set `timelapse_retention_days` and `timelapse_interval_seconds` appropriately

## Restrospec
- I am not using features I thought I needed (currently).  Such as Mister or Fan, as a result I have disconnected the relay's as they were not required.  Leaving it here as it is an option.
- I am not entirely happy with the LCD enclosure, and sensor enclosures.  But they work.  I used Crazy Glue to hold parts in place such as buttons and the sensor top/bottom.
- External weather is something I thought interesting to log, not sure yet how much I will value this data.
- Originally all my sensors , LCD are connected with project pins/wires.  I found that USB-C connectors are cheap, support up to 16 pins, so I am going to build a new setup with USB-C connectors (confusing for some but this way if you need longer cables...just buy a new USB-C cable)

---

*Built with way too much enthusiasm for a gecko that probably doesn't care.*

LCD Screen:

<img width="429" height="456" alt="image" src="https://github.com/user-attachments/assets/99f5f539-55a7-4309-8ab4-bcee95bf8aa7" />


Main Website:
<img width="1549" height="885" alt="image" src="https://github.com/user-attachments/assets/e6784ec4-9f8d-4bc0-b9d2-dd6a82b89407" />
<img width="1554" height="753" alt="image" src="https://github.com/user-attachments/assets/f91a0f3e-a688-4392-9271-b7c5c5f8e029" />
<img width="1552" height="745" alt="image" src="https://github.com/user-attachments/assets/b04e211b-7818-469e-9037-8371b906c45b" />


Configuration Editor:
<img width="1558" height="944" alt="image" src="https://github.com/user-attachments/assets/e8f28b19-e6ad-443a-9a04-fa0943a4852a" />

TimeLapse:
<img width="1521" height="494" alt="image" src="https://github.com/user-attachments/assets/88954f4a-ce45-456d-8eab-79b22149b35a" />





