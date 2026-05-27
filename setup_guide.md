# 🦎 Terrarium Monitor – Setup Guide

## Hardware Required

| Component | Notes |
|-----------|-------|
| Raspberry Pi 4 (or 3B+) | Running Raspberry Pi OS Lite (64-bit recommended) |
| 2× SHT31-D sensor modules | I²C; 0x44 (ADDR→GND) and 0x45 (ADDR→3.3V) |
| 2× 5V Relay modules | Active-LOW (most common relay boards) |
| Waveshare 2" ST7789 SPI display | 240×320, SPI0 |
| 2× TP-Link Tapo smart plugs | P100/P105/P110 on same LAN |
| TP-Link Tapo camera | C100 or similar on same LAN |


---

## Wiring

### SHT31 Sensors (I²C)

```
Sensor 1 (addr 0x44)  →  ADDR pin → GND
Sensor 2 (addr 0x45)  →  ADDR pin → 3.3V

Both sensors:
  VCC  → Pin 1  (3.3V)
  GND  → Pin 6  (GND)
  SDA  → Pin 3  (GPIO2)
  SCL  → Pin 5  (GPIO3)
```

### Relay – Mister (GPIO 17)
```
IN   → Pin 11 (GPIO17)
VCC  → Pin 2  (5V)
GND  → Pin 9  (GND)
```

### Relay – Fan (GPIO 27)
```
IN   → Pin 13 (GPIO27)
VCC  → Pin 4  (5V)
GND  → Pin 14 (GND)
```

### Waveshare 2" ST7789 Display
```
Display  →  Pi Header
VCC      →  Pin 17 (3.3V)
GND      →  Pin 20 (GND)
DIN      →  Pin 19 (GPIO10 / MOSI)
CLK      →  Pin 23 (GPIO11 / SCLK)
CS       →  Pin 24 (GPIO8  / CE0)
DC       →  Pin 18 (GPIO24)
RST      →  Pin 22 (GPIO25)
BL       →  Pin 12 (GPIO18 / PWM0)
```

---

## Software Setup

### 1. Enable interfaces on the Pi
```bash
sudo raspi-config
# Interface Options → I2C → Enable
# Interface Options → SPI → Enable
sudo reboot
```

### 2. Clone / copy project files
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

pip install --upgrade pip
pip install \
  adafruit-circuitpython-sht31d \
  RPi.GPIO \
  Pillow \
  st7789 \
  flask \
  PyP100 \
  onvif-zeep \
  opencv-python-headless \
  requests
```

> **Note on PyP100**: If your TAPO plugs have newer firmware (≥ 1.2.x),
> they require authentication. In that case install `tapo` instead:
> `pip install tapo` and update `tapo_controller.py` accordingly.
> Older firmware works with no credentials via PyP100.

### 4. Edit config.ini
```bash
nano config.ini
```
Key settings to update:
- `[tapo_plug_1]` / `[tapo_plug_2]` → set correct **IP addresses**
- `[display]` → set `temp_unit = F` or `C`
- `[mister]` / `[fan]` → tune thresholds and runtimes
- `[general]` → set your **timezone**

### 5. Test run
```bash
source venv/bin/activate
python main.py
```
Open browser to `http://<pi-ip>:8080` to see the dashboard.

### 6. Install as a systemd service (auto-start on boot)
```bash
sudo cp terrarium.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable terrarium
sudo systemctl start terrarium

# Check logs
sudo journalctl -u terrarium -f
```

---

## Adding a Third (or More) Sensors

1. Wire the new SHT31 to I²C — if same bus, you'll need a TCA9548A I²C multiplexer
   (only two addresses available on one bus: 0x44 and 0x45).
2. Add a new section to `config.ini`:
```ini
[sensor_3]
enabled = true
name = Basking Spot
i2c_address = 0x44
i2c_bus = 3       ; secondary bus via TCA9548A or second I2C port
```
3. Restart the service — no code changes needed.

---

## Mister / Fan Logic Summary

```
Every poll_interval_seconds:
  Read all sensors → average humidity / temperature

  MISTER:
    if avg_humidity < humidity_threshold_low
    AND not running AND not in cooldown
    AND 24h run count < mister_max_runs_per_24h:
      → Turn ON for mister_runtime_minutes
      → Wait mister_monitor_delay_minutes (cooldown)
      → Re-evaluate

  FAN:
    if avg_temp > temp_threshold_high  (too hot)
    OR avg_temp < temp_threshold_low   (too cold)
    AND not running AND not in cooldown
    AND 24h run count < fan_max_runs_per_24h:
      → Turn ON for fan_runtime_minutes
      → Wait fan_monitor_delay_minutes (cooldown)
      → Re-evaluate

  LIGHTS:
    Background thread checks every 60 s:
    if current time is between on_time and off_time → plug ON
    else → plug OFF
```

---

## Web Dashboard

| URL | Description |
|-----|-------------|
| `http://<pi-ip>:8080/` | Live dashboard |
| `http://<pi-ip>:8080/api/status` | JSON snapshot |
| `http://<pi-ip>:8080/api/history?hours=24` | Sensor history |
| `POST /api/relay/Mister/on` | Force mister on |
| `POST /api/relay/Fan/off` | Force fan off |
| `POST /api/light/UVB Light/on` | Force plug on |

To enable login, set `auth_enabled = true` in `[web]` and set `username` / `password`.

---

## File Structure

```
terrarium/
├── config.ini
├── main.py
├── sensor_manager.py
├── data_logger.py
├── relay_controller.py
├── tapo_controller.py
├── display_manager.py
├── web_dashboard.py
├── terrarium.service
├── data/
│   ├── terrarium.db    ← SQLite database (auto-created)
│   └── terrarium.log   ← rolling log (auto-created)
└── venv/
```
