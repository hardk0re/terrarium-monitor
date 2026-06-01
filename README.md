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

- **Dual SHT31-D sensor monitoring** — temperature and humidity from up to N sensors (expandable via config)
- **ST7789 2" display** — cycles through indoor sensors, outdoor weather, and system status
- **Colour-coded readings** — green = good range, blue = too cold/dry, red = too hot/humid, yellow = sensor error
- **Misting control** — 5V relay triggers automatically when humidity drops below threshold, with configurable runtime, cooldown delay, and 24-hour run cap
- **Fan control** — separate 5V relay for high/low temperature, same configurable runtime and cooldown system
- **TAPO/KASA smart plug lighting** — local API control, no cloud required, fully scheduled (on/off time per plug)
- **TAPO camera** — ONVIF snapshot every 30 seconds, live image in web dashboard, optional timelapse with configurable retention
- **Outdoor weather** — OpenWeatherMap integration, 24h history chart, shows on display cycle
- **Web dashboard** — live sensor data, charts, relay overrides, light controls, camera snapshot, feeding/care log, config editor
- **Feeding log** — one-tap buttons for configurable food items (crickets, waxworms, paste, etc.)
- **Care log** — one-tap buttons for configurable care activities (cleaning, misting, substrate change, vet visit, etc.)
- **Physical buttons** — up to three optional momentary push-buttons wired to GPIO pins, each with its own configurable log category (`care`, `feeding`, etc.) and message — handy when your hands are wet from misting and the web UI isn't convenient (one press → one log entry → Pushover if that category is enabled)
- **SQLite logging** — all sensor readings, weather, relay events, and care/feeding entries stored locally with configurable retention (default 31 days)
- **Config editor** — edit any config value from the web UI without touching the Pi
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
Sensor 1 (0x44) → ADDR pin to GND
Sensor 2 (0x45) → ADDR pin to 3.3V

Both sensors:
  VCC → Pin 1  (3.3V)
  GND → Pin 6  (GND)
  SDA → Pin 3  (GPIO2)
  SCL → Pin 5  (GPIO3)
```

### Relay – Mister (GPIO 17)
```
IN  → Pin 11 (GPIO17)
VCC → Pin 2  (5V)
GND → Pin 9  (GND)
```

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

```
Button 1: GPIO23  (Pin 16) ─┐
Button 2: GPIO24  (Pin 18) ─┼──── other leg → any GND pin (e.g. Pin 14)
Button 3: GPIO25  (Pin 22) ─┘
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
nano config.ini
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
├── config.ini              ← all settings
├── main.py                 ← entry point
├── sensor_manager.py       ← SHT31 sensor reading
├── data_logger.py          ← SQLite storage
├── relay_controller.py     ← GPIO relay control
├── tapo_controller.py      ← smart plug control
├── display_manager.py      ← ST7789 display driver
├── camera_manager.py       ← TAPO ONVIF camera
├── weather_manager.py      ← OpenWeatherMap
├── web_dashboard.py        ← Flask web UI
├── care_button.py          ← GPIO push-buttons (up to 3) → log entries
├── terrarium.service       ← systemd unit
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
| `http://<pi-ip>:8080/config` | Config editor (password protected) |
| `http://<pi-ip>:8080/api/status` | JSON status snapshot |
| `http://<pi-ip>:8080/api/history?hours=24` | Sensor history |
| `http://<pi-ip>:8080/api/weather/history` | Weather history |
| `POST /api/relay/Mister/on` | Force mister on |
| `POST /api/relay/Fan/off` | Force fan off |
| `POST /api/light/UVB%20Light/on` | Force plug on |

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

---

## TODO

- [x] Physical buttons on the Pi for feeding/care logging without needing the web UI ([care_button.py](care_button.py))
- [x] Push notifications via Pushover ([pushover_notifier.py](pushover_notifier.py))
- [x] Timelapse video review
- [ ] Multi-enclosure support
- [ ] Create 3D STL for Display / Enclosure / Sensor Mounts
- [ ] Actually waiting on sensors so haven't even tested THAT code yet! :)

---

## Notes

- The TAPO camera requires ONVIF to be enabled in the Tapo app under **Advanced Settings → ONVIF**
- Smart plugs on firmware ≥ 1.2.x may require credentials — update `tapo_controller.py` accordingly
- The web config editor saves immediately but some changes (GPIO pins, I²C addresses, web port) require a restart to take effect
- All data is stored locally in SQLite — nothing leaves your network
- Timelapse frames accumulate quickly; set `timelapse_retention_days` and `timelapse_interval_seconds` appropriately

---

*Built with way too much enthusiasm for a gecko that probably doesn't care.*

Main Website:
<img width="1424" height="999" alt="image" src="https://github.com/user-attachments/assets/fcfbd002-577e-41e7-894e-7978cc61e8a8" />
<img width="1443" height="722" alt="image" src="https://github.com/user-attachments/assets/a5a05bbc-cbbd-46b7-aec5-7238c09dfaa0" />


Configuration Editor:
<img width="1617" height="1035" alt="image" src="https://github.com/user-attachments/assets/aa1e9c56-a878-49ea-a921-ab83961066b2" />



