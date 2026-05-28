"""
display_manager.py
Custom ST7789 driver for Waveshare 2" 240x320 IPS display.
Bypasses the pip st7789 library entirely — uses raw SPI + correct init sequence.

Fixes applied from hardware testing:
  - Display inversion ON (0x21)
  - MADCTL 0x00
  - No offsets needed
  - RGB565 big-endian byte order
"""

import time
import logging
import threading
import configparser
from datetime import datetime
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_OK = True
except ImportError:
    _PIL_OK = False
    logging.warning("Pillow not installed.")

try:
    import spidev
    import RPi.GPIO as GPIO
    _HW_OK = True
except ImportError:
    _HW_OK = False
    logging.warning("spidev or RPi.GPIO not available – display disabled.")

logger = logging.getLogger(__name__)

# Pins (BCM)
_DC  = 24
_RST = 25
_BL  = 18

# Display dimensions
W, H = 240, 320

# Fallback colour palette
BG        = (10,  10,  20)
ACCENT    = (0,   200, 120)
WARN_HOT  = (220, 60,  20)
WARN_COLD = (40,  120, 255)
GOOD      = (20,  200, 60)
WARN_ERR  = (255, 200, 0)
WHITE     = (255, 255, 255)
GREY      = (130, 130, 150)
RELAY_ON  = (255, 200, 0)
RELAY_OFF = (60,  60,  80)

# Fonts
FONT_DIR  = Path("/usr/share/fonts/truetype/dejavu")
FONT_BOLD = str(FONT_DIR / "DejaVuSans-Bold.ttf")
FONT_REG  = str(FONT_DIR / "DejaVuSans.ttf")


def _parse_color(val: str, fallback: tuple) -> tuple:
    try:
        parts = [int(x.strip()) for x in val.split(",")]
        if len(parts) == 3:
            return tuple(parts)
    except Exception:
        pass
    return fallback


def _load_font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


# ======================================================================
# Raw ST7789 driver
# ======================================================================

class ST7789Driver:
    def __init__(self, dc=_DC, rst=_RST, bl=_BL, spi_speed=40_000_000, brightness=80):
        self.dc          = dc
        self.rst         = rst
        self.bl          = bl
        self._brightness = max(0, min(100, brightness))

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        for p in [dc, rst, bl]:
            GPIO.setup(p, GPIO.OUT)

        self._spi = spidev.SpiDev()
        self._spi.open(0, 0)
        self._spi.max_speed_hz = spi_speed
        self._spi.mode = 0

        self._init_display()

    def _cmd(self, c):
        GPIO.output(self.dc, GPIO.LOW)
        self._spi.writebytes([c])

    def _data(self, d):
        GPIO.output(self.dc, GPIO.HIGH)
        if isinstance(d, int):
            d = [d]
        for i in range(0, len(d), 4096):
            self._spi.writebytes(d[i:i+4096])

    def _reset(self):
        for v in [GPIO.HIGH, GPIO.LOW, GPIO.HIGH]:
            GPIO.output(self.rst, v)
            time.sleep(0.1)

    def _init_display(self):
        # Set up backlight PWM for brightness control
        GPIO.output(self.bl, GPIO.HIGH)
        self._pwm = GPIO.PWM(self.bl, 1000)  # 1kHz PWM
        self._pwm.start(self._brightness)

        self._reset()
        self._cmd(0x36); self._data(0x00)
        self._cmd(0x3A); self._data(0x05)
        self._cmd(0xB2); self._data([0x0C, 0x0C, 0x00, 0x33, 0x33])
        self._cmd(0xB7); self._data(0x35)
        self._cmd(0xBB); self._data(0x19)
        self._cmd(0xC0); self._data(0x2C)
        self._cmd(0xC2); self._data(0x01)
        self._cmd(0xC3); self._data(0x12)
        self._cmd(0xC4); self._data(0x20)
        self._cmd(0xC6); self._data(0x0F)
        self._cmd(0xD0); self._data([0xA4, 0xA1])
        self._cmd(0x11); time.sleep(0.12)
        self._cmd(0x21)                   # inversion ON
        self._cmd(0x29); time.sleep(0.05) # display on
        logger.info("ST7789 display initialised at %d%% brightness.", self._brightness)

    def set_brightness(self, percent: int):
        """Set backlight brightness 0-100."""
        self._brightness = max(0, min(100, percent))
        if hasattr(self, '_pwm'):
            self._pwm.ChangeDutyCycle(self._brightness)
            logger.info("Display brightness set to %d%%", self._brightness)

    def display(self, img: "Image.Image"):
        if img.size != (W, H):
            img = img.resize((W, H))
        pixels = list(img.getdata())
        buf = []
        for r, g, b in pixels:
            rgb = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            buf.append((rgb >> 8) & 0xFF)
            buf.append(rgb & 0xFF)
        self._cmd(0x2A); self._data([0x00, 0x00, 0x00, 0xEF])
        self._cmd(0x2B); self._data([0x00, 0x00, 0x01, 0x3F])
        self._cmd(0x2C)
        self._data(buf)

    def clear(self, color=(0, 0, 0)):
        self.display(Image.new("RGB", (W, H), color))

    def cleanup(self):
        self.clear()
        if hasattr(self, '_pwm'):
            self._pwm.stop()
        GPIO.output(self.bl, GPIO.LOW)
        self._spi.close()


# ======================================================================
# Display Manager
# ======================================================================

class DisplayManager:
    def __init__(self, config: configparser.ConfigParser,
                 sensor_manager=None, mister=None, fan=None,
                 lighting=None, weather=None):
        self.config         = config
        self.cfg            = config["display"]
        self.sensor_manager = sensor_manager
        self.mister         = mister
        self.fan            = fan
        self.lighting       = lighting
        self.weather        = weather
        self._page          = 0
        self._running       = False
        self._latest        = []
        self._lock          = threading.Lock()
        self._disp          = None

        self._load_config_values()

        if not self.cfg.getboolean("enabled", fallback=True) or not _HW_OK or not _PIL_OK:
            logger.info("Display disabled or libraries missing.")
            return

        try:
            self._disp = ST7789Driver(
                dc         = self.cfg.getint("dc_pin",    fallback=24),
                rst        = self.cfg.getint("rst_pin",   fallback=25),
                bl         = self.cfg.getint("bl_pin",    fallback=18),
                spi_speed  = 40_000_000,
                brightness = self.cfg.getint("brightness", fallback=80),
            )
        except Exception as e:
            logger.error("Display init failed: %s", e)
            return

        self._fnt_large = _load_font(FONT_BOLD, 52)
        self._fnt_med   = _load_font(FONT_BOLD, 26)
        self._fnt_small = _load_font(FONT_REG,  18)
        self._fnt_tiny  = _load_font(FONT_REG,  14)
        self._disp.clear(self.col_bg)

    # ------------------------------------------------------------------
    # Config loader
    # ------------------------------------------------------------------

    def _load_config_values(self):
        c = self.cfg
        self.temp_unit      = c.get("temp_unit", "F").upper()
        self.page_cycle_sec = c.getfloat("page_cycle_seconds", fallback=5)
        self.brightness     = c.getint("brightness", fallback=80)
        self.temp_low       = c.getfloat("temp_low",             fallback=75.0)
        self.temp_high      = c.getfloat("temp_high",            fallback=85.0)
        self.hum_low        = c.getfloat("humidity_display_low",  fallback=50.0)
        self.hum_high       = c.getfloat("humidity_display_high", fallback=80.0)

        self.col_bg        = _parse_color(c.get("color_background", ""), BG)
        self.col_accent    = _parse_color(c.get("color_accent",     ""), ACCENT)
        self.col_grey      = _parse_color(c.get("color_grey",       ""), GREY)
        self.col_relay_on  = _parse_color(c.get("color_relay_on",   ""), RELAY_ON)
        self.col_relay_off = _parse_color(c.get("color_relay_off",  ""), RELAY_OFF)
        self.col_temp_cold = _parse_color(c.get("color_temp_cold",  ""), WARN_COLD)
        self.col_temp_good = _parse_color(c.get("color_temp_good",  ""), GOOD)
        self.col_temp_hot  = _parse_color(c.get("color_temp_hot",   ""), WARN_HOT)
        self.col_temp_err  = _parse_color(c.get("color_temp_error", ""), WARN_ERR)
        self.col_hum_low   = _parse_color(c.get("color_hum_low",    ""), WARN_COLD)
        self.col_hum_good  = _parse_color(c.get("color_hum_good",   ""), GOOD)
        self.col_hum_high  = _parse_color(c.get("color_hum_high",   ""), WARN_HOT)
        self.col_hum_err   = _parse_color(c.get("color_hum_error",  ""), WARN_ERR)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_readings(self, readings):
        with self._lock:
            self._latest = readings

    def update_components(self, sensor_manager=None, mister=None,
                          fan=None, lighting=None, weather=None):
        """Hot-swap subsystem references after a config reload."""
        with self._lock:
            if sensor_manager is not None: self.sensor_manager = sensor_manager
            if mister         is not None: self.mister         = mister
            if fan            is not None: self.fan            = fan
            if lighting       is not None: self.lighting       = lighting
            if weather        is not None: self.weather        = weather
        self.cfg = self.config["display"]
        self._load_config_values()
        if self._disp:
            self._disp.set_brightness(self.brightness)
        logger.info("Display components updated.")

    def start(self):
        if not self._disp:
            return
        self._running = True
        threading.Thread(target=self._loop, daemon=True, name="display").start()
        logger.info("Display loop started.")

    def stop(self):
        self._running = False
        if self._disp:
            self._show_shutdown()
            self._disp.cleanup()

    # ------------------------------------------------------------------
    # Page loop
    # ------------------------------------------------------------------

    def _loop(self):
        while self._running:
            with self._lock:
                readings = list(self._latest)

            pages = [self._draw_all_sensors_page(readings)]

            if self.weather and self.weather.enabled:
                w = self.weather.latest()
                if w:
                    pages.append(self._draw_weather_page(w))

            pages.append(self._draw_status_page())

            self._disp.display(pages[self._page % len(pages)])
            self._page += 1
            time.sleep(self.page_cycle_sec)

    # ------------------------------------------------------------------
    # Colour helpers
    # ------------------------------------------------------------------

    def _temp_color(self, temp_c):
        try:
            if self.temp_unit == "F":
                lo_c = (self.temp_low  - 32) * 5 / 9
                hi_c = (self.temp_high - 32) * 5 / 9
            else:
                lo_c, hi_c = self.temp_low, self.temp_high
            if temp_c < lo_c: return self.col_temp_cold
            if temp_c > hi_c: return self.col_temp_hot
            return self.col_temp_good
        except Exception:
            return WHITE

    def _hum_color(self, humidity):
        try:
            if humidity < self.hum_low:  return self.col_hum_low
            if humidity > self.hum_high: return self.col_hum_high
            return self.col_hum_good
        except Exception:
            return WHITE

    # ------------------------------------------------------------------
    # Screen builders
    # ------------------------------------------------------------------

    def _new_canvas(self):
        img = Image.new("RGB", (W, H), self.col_bg)
        return img, ImageDraw.Draw(img)

    def _draw_all_sensors_page(self, readings) -> Image.Image:
        img, d = self._new_canvas()

        d.rectangle([(0, 0), (W, 34)], fill=self.col_accent)
        d.text((8, 7),    "Terrarium Monitor",              font=self._fnt_small, fill=self.col_bg)
        d.text((W-58, 7), datetime.now().strftime("%H:%M"), font=self._fnt_small, fill=self.col_bg)

        rows = []
        if readings:
            for r in readings:
                rows.append({
                    "name":         r.name,
                    "temp_c":       r.temperature_c,
                    "temp_display": r.temperature_f if self.temp_unit == "F" else r.temperature_c,
                    "humidity":     r.humidity,
                    "error":        False,
                })
        else:
            for section in self.config.sections():
                if section.lower().startswith("sensor_"):
                    scfg = self.config[section]
                    if scfg.getboolean("enabled", fallback=True):
                        rows.append({
                            "name":         scfg.get("name", section),
                            "temp_c":       None,
                            "temp_display": None,
                            "humidity":     None,
                            "error":        True,
                        })
            if not rows:
                rows.append({"name": "No sensors configured",
                             "temp_c": None, "temp_display": None,
                             "humidity": None, "error": True})

        available = H - 34 - 30
        row_h     = available // max(len(rows), 1)

        for i, row in enumerate(rows):
            y  = 34 + i * row_h
            ty = y + 26

            bar_col = (60, 20, 20) if row["error"] else (30, 30, 60)
            d.rectangle([(0, y), (W, y+22)], fill=bar_col)
            d.text((8, y+4), row["name"], font=self._fnt_tiny,
                   fill=self.col_temp_err if row["error"] else self.col_accent)

            if row["error"]:
                d.text((8,      ty),    "TEMP",     font=self._fnt_tiny, fill=self.col_grey)
                d.text((8,      ty+14), "N/A",      font=self._fnt_med,  fill=self.col_temp_err)
                d.text((W//2+8, ty),    "HUMIDITY", font=self._fnt_tiny, fill=self.col_grey)
                d.text((W//2+8, ty+14), "N/A",      font=self._fnt_med,  fill=self.col_hum_err)
                d.text((8,      ty+42), "! Sensor disconnected",
                       font=self._fnt_tiny, fill=self.col_temp_err)
            else:
                unit_str = "°F" if self.temp_unit == "F" else "°C"
                t_col    = self._temp_color(row["temp_c"])
                h_col    = self._hum_color(row["humidity"])
                d.text((8,      ty),    "TEMP",                                font=self._fnt_tiny, fill=self.col_grey)
                d.text((8,      ty+14), f"{row['temp_display']:.1f}{unit_str}", font=self._fnt_med,  fill=t_col)
                d.text((W//2+8, ty),    "HUMIDITY",                            font=self._fnt_tiny, fill=self.col_grey)
                d.text((W//2+8, ty+14), f"{row['humidity']:.1f}%",             font=self._fnt_med,  fill=h_col)

            if i < len(rows) - 1:
                d.line([(8, y+row_h-2), (W-8, y+row_h-2)], fill=(40, 40, 70), width=1)

        self._draw_relay_strip(d, y=H-26)
        return img

    def _draw_weather_page(self, w: dict) -> Image.Image:
        img, d = self._new_canvas()

        d.rectangle([(0, 0), (W, 34)], fill=(20, 40, 80))
        d.text((8, 7),    "Outdoor Weather",                font=self._fnt_small, fill=WHITE)
        d.text((W-58, 7), datetime.now().strftime("%H:%M"), font=self._fnt_small, fill=self.col_grey)

        city = w.get("city", "")
        if city:
            d.text((8, 42), city, font=self._fnt_tiny, fill=self.col_grey)

        # Use weather section temp_unit if set, else fall back to display temp_unit
        try:
            wx_unit = self.config.get("weather", "temp_unit", fallback=None)
            unit_str = (wx_unit or self.temp_unit).upper()
        except Exception:
            unit_str = self.temp_unit

        temp_c = w["temp_c"]
        temp_val = temp_c * 9 / 5 + 32 if unit_str == "F" else temp_c

        d.text((8, 60), "TEMP",                       font=self._fnt_tiny,  fill=self.col_grey)
        d.text((8, 74), f"{temp_val:.1f}°{unit_str}", font=self._fnt_large, fill=(255, 180, 0))

        fl_c = w.get("feels_like_c", temp_c)
        fl   = fl_c * 9 / 5 + 32 if unit_str == "F" else fl_c
        d.text((8, 132), f"Feels like {fl:.1f}°{unit_str}", font=self._fnt_tiny, fill=self.col_grey)

        d.line([(8, 150), (W-8, 150)], fill=(40, 40, 70), width=1)

        hum = w.get("humidity", 0)
        d.text((8,      158), "HUMIDITY",          font=self._fnt_tiny, fill=self.col_grey)
        d.text((8,      172), f"{hum}%",           font=self._fnt_med,  fill=(80, 160, 255))

        wind_kmh = w.get("wind_speed", 0) * 3.6
        d.text((W//2+8, 158), "WIND",                   font=self._fnt_tiny, fill=self.col_grey)
        d.text((W//2+8, 172), f"{wind_kmh:.1f} km/h",  font=self._fnt_med,  fill=WHITE)

        desc = w.get("description", "")
        d.text((8, 210), desc, font=self._fnt_small, fill=WHITE)

        d.line([(8, 248), (W-8, 248)], fill=(40, 40, 70), width=1)
        self._draw_relay_strip(d, y=H-26)
        return img

    def _draw_status_page(self) -> Image.Image:
        img, d = self._new_canvas()

        d.rectangle([(0, 0), (W, 38)], fill=(40, 40, 80))
        d.text((8, 6),    "System Status",               font=self._fnt_small, fill=WHITE)
        d.text((W-60, 6), datetime.now().strftime("%H:%M"), font=self._fnt_small, fill=self.col_grey)

        y = 48
        for label, relay in [("Mister", self.mister), ("Fan", self.fan)]:
            if relay:
                col  = self.col_relay_on if relay.is_on else self.col_relay_off
                cool = " (cooldown)" if relay.in_cooldown else ""
                txt  = f"{label}: {'ON' if relay.is_on else 'OFF'}{cool}"
                d.rectangle([(8, y), (W-8, y+30)], fill=col)
                d.text((14, y+6), txt, font=self._fnt_small,
                       fill=self.col_bg if relay.is_on else WHITE)
                y += 38

        if self.lighting:
            for plug in self.lighting.status():
                col = self.col_relay_on if plug["state"] else self.col_relay_off
                txt = f"{plug['name']}: {plug['on_time']}–{plug['off_time']}"
                d.rectangle([(8, y), (W-8, y+30)], fill=col)
                d.text((14, y+6), txt, font=self._fnt_small,
                       fill=self.col_bg if plug["state"] else WHITE)
                y += 38
                if y > H - 20:
                    break
        return img

    def _draw_relay_strip(self, d, y):
        items = []
        if self.mister: items.append(("MIST", self.mister.is_on))
        if self.fan:    items.append(("FAN",  self.fan.is_on))
        if self.lighting:
            for p in self.lighting.status():
                items.append((p["name"][:4].upper(), p["state"]))
        x = 12
        for label, state in items:
            col = self.col_relay_on if state else self.col_relay_off
            d.rectangle([(x, y), (x+48, y+22)], fill=col)
            d.text((x+4, y+4), label, font=self._fnt_tiny,
                   fill=self.col_bg if state else WHITE)
            x += 56

    def _draw_no_sensor_page(self) -> Image.Image:
        img, d = self._new_canvas()
        d.text((20, 140), "No sensors found", font=self._fnt_small, fill=self.col_temp_err)
        return img

    def _show_shutdown(self):
        img, d = self._new_canvas()
        d.text((50, 140), "Shutting down...", font=self._fnt_small, fill=self.col_grey)
        if self._disp:
            self._disp.display(img)