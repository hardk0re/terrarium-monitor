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
    def __init__(self, dc=_DC, rst=_RST, bl=_BL, spi_speed=40_000_000):
        self.dc  = dc
        self.rst = rst
        self.bl  = bl
        self._bl_pwm = None

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        for p in [dc, rst, bl]:
            GPIO.setup(p, GPIO.OUT)

        # Backlight via software PWM so we can control brightness.
        # 200 Hz is well above visible flicker. Fall back to on/off if PWM fails.
        try:
            self._bl_pwm = GPIO.PWM(self.bl, 200)
            self._bl_pwm.start(100)
        except Exception as e:
            logger.warning("Backlight PWM unavailable; falling back to on/off: %s", e)
            GPIO.output(self.bl, GPIO.HIGH)

        self._spi = spidev.SpiDev()
        self._spi.open(0, 0)
        self._spi.max_speed_hz = spi_speed
        self._spi.mode = 0

        self._init_display()

    def set_brightness(self, percent: int):
        p = max(0, min(100, int(percent)))
        if self._bl_pwm:
            self._bl_pwm.ChangeDutyCycle(p)
        else:
            GPIO.output(self.bl, GPIO.HIGH if p > 0 else GPIO.LOW)

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
        logger.info("ST7789 display initialised.")

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
        if self._bl_pwm:
            try:
                self._bl_pwm.stop()
            except Exception:
                pass
        GPIO.output(self.bl, GPIO.LOW)
        self._spi.close()


# ======================================================================
# Display Manager
# ======================================================================

class DisplayManager:
    def __init__(self, config: configparser.ConfigParser,
                 sensor_manager=None, mister=None, fan=None,
                 lighting=None, weather=None, data_logger=None):
        self.config         = config
        self.cfg            = config["display"]
        self.sensor_manager = sensor_manager
        self.mister         = mister
        self.fan            = fan
        self.lighting       = lighting
        self.weather        = weather
        self.data_logger    = data_logger
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
                dc        = self.cfg.getint("dc_pin",  fallback=24),
                rst       = self.cfg.getint("rst_pin", fallback=25),
                bl        = self.cfg.getint("bl_pin",  fallback=18),
                spi_speed = 40_000_000,
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
        self.brightness_day   = c.getint("brightness",       fallback=80)
        self.brightness_night = c.getint("night_brightness", fallback=15)
        self.night_start = self._parse_hhmm(c.get("night_start", fallback="22:00"))
        self.night_end   = self._parse_hhmm(c.get("night_end",   fallback="07:00"))
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
    # Brightness / night-mode helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_hhmm(s: str) -> tuple:
        """Parse 'HH:MM' to an (hour, minute) tuple. Tuple comparison handles ordering."""
        try:
            h, m = s.strip().split(":")
            return (int(h) % 24, int(m) % 60)
        except Exception:
            return (0, 0)

    def _is_night(self) -> bool:
        now = datetime.now()
        cur = (now.hour, now.minute)
        # Wrap-around case (e.g. 22:00 → 07:00): night is "after start OR before end"
        if self.night_start > self.night_end:
            return cur >= self.night_start or cur < self.night_end
        # Same-day case (e.g. 13:00 → 15:00 for a midday "siesta"): in [start, end)
        return self.night_start <= cur < self.night_end

    def _apply_brightness(self):
        if not self._disp:
            return
        target = self.brightness_night if self._is_night() else self.brightness_day
        if target != getattr(self, "_last_brightness", None):
            try:
                self._disp.set_brightness(target)
                self._last_brightness = target
                logger.info("Display brightness → %d%% (%s)", target,
                            "night" if self._is_night() else "day")
            except Exception as e:
                logger.warning("Failed to set brightness: %s", e)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_readings(self, readings):
        with self._lock:
            self._latest = readings

    def update_components(self, sensor_manager=None, mister=None,
                          fan=None, lighting=None, weather=None,
                          data_logger=None):
        """Hot-swap subsystem references after a config reload."""
        with self._lock:
            if sensor_manager is not None: self.sensor_manager = sensor_manager
            if mister         is not None: self.mister         = mister
            if fan            is not None: self.fan            = fan
            if lighting       is not None: self.lighting       = lighting
            if weather        is not None: self.weather        = weather
            if data_logger    is not None: self.data_logger    = data_logger
        self.cfg = self.config["display"]
        self._load_config_values()
        self._last_brightness = None  # force re-apply on next loop tick
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
            self._apply_brightness()

            with self._lock:
                readings = list(self._latest)

            pages = [self._draw_all_sensors_page(readings)]

            if self.weather and self.weather.enabled:
                w = self.weather.latest()
                if w:
                    pages.append(self._draw_weather_page(w))

            pages.append(self._draw_status_page())

            mood_page = self._maybe_draw_gecko_mood()
            if mood_page is not None:
                pages.append(mood_page)

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
        d.text((8, 7),    "Outdoor Weather",              font=self._fnt_small, fill=WHITE)
        d.text((W-58, 7), datetime.now().strftime("%H:%M"), font=self._fnt_small, fill=self.col_grey)

        city = w.get("city", "")
        if city:
            d.text((8, 42), city, font=self._fnt_tiny, fill=self.col_grey)

        temp_c = w["temp_c"]
        if self.temp_unit == "F":
            temp_val = temp_c * 9 / 5 + 32
            unit_str = "°F"
        else:
            temp_val = temp_c
            unit_str = "°C"

        d.text((8, 60), "TEMP",                        font=self._fnt_tiny,  fill=self.col_grey)
        d.text((8, 74), f"{temp_val:.1f}{unit_str}",   font=self._fnt_large, fill=(255, 180, 0))

        fl_c = w.get("feels_like_c", temp_c)
        fl   = fl_c * 9 / 5 + 32 if self.temp_unit == "F" else fl_c
        d.text((8, 132), f"Feels like {fl:.1f}{unit_str}", font=self._fnt_tiny, fill=self.col_grey)

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

    def _maybe_draw_gecko_mood(self):
        """Show a gecko mood page when the gecko isn't happy (neutral or upset).
        Returns None if disabled or when everything is fine."""
        if not self.data_logger:
            return None
        try:
            from gecko_mood import compute_mood
            mood = compute_mood(self.config, self.data_logger)
        except Exception as e:
            logger.debug("gecko_mood failed: %s", e)
            return None
        # Show whenever the gecko isn't happy — neutral (yellow) or upset (red).
        if not mood.get("enabled") or mood.get("mood") not in ("neutral", "upset"):
            return None
        return self._draw_gecko_mood_page(mood)

    def _draw_gecko_mood_page(self, mood: dict) -> "Image.Image":
        palette = {
            "happy":   {"body": (0,  200, 120), "bg": (8,  28,  18)},
            "neutral": {"body": (255, 200, 0),  "bg": (32, 28,  10)},
            "upset":   {"body": (220, 60,  20), "bg": (40, 14,  10)},
        }
        p = palette.get(mood.get("mood", "upset"), palette["upset"])
        body, bg = p["body"], p["bg"]

        img, d = self._new_canvas()
        d.rectangle([(0, 0), (W, H)], fill=bg)

        # Title bar
        d.text((10, 10), "GECKO MOOD", font=self._fnt_small, fill=WHITE)
        label = mood["mood"].upper()
        try:
            bbox = d.textbbox((0, 0), label, font=self._fnt_small)
            lw = bbox[2] - bbox[0]
        except Exception:
            lw = len(label) * 10
        d.rectangle([(W - lw - 22, 6), (W - 10, 30)], fill=body)
        d.text((W - lw - 16, 10), label, font=self._fnt_small, fill=(0, 0, 0))

        # Gecko, drawn centered. The drawing fits inside ~200x110.
        self._draw_gecko(d, x=20, y=70, body=body, mood=mood["mood"])

        # Mood headline
        headline = {"happy": "I'm doing great!",
                    "neutral": "I'm a bit off",
                    "upset": "I need attention!"}.get(mood["mood"], "")
        try:
            bbox = d.textbbox((0, 0), headline, font=self._fnt_med)
            hw = bbox[2] - bbox[0]
        except Exception:
            hw = len(headline) * 14
        d.text(((W - hw) // 2, 200), headline, font=self._fnt_med, fill=body)

        # Reasons — show only the ones bringing the mood down
        worst = min((r["score"] for r in mood.get("reasons", [])), default=2)
        offenders = [r["label"] for r in mood.get("reasons", []) if r["score"] == worst]
        y = 240
        for line in offenders[:3]:
            line = line[:32]  # avoid overflow
            try:
                bbox = d.textbbox((0, 0), line, font=self._fnt_tiny)
                tw = bbox[2] - bbox[0]
            except Exception:
                tw = len(line) * 7
            d.text(((W - tw) // 2, y), line, font=self._fnt_tiny, fill=WHITE)
            y += 18

        return img

    def _draw_gecko(self, d, x: int, y: int, body: tuple, mood: str):
        """Draw a stylized gecko at (x,y) inside a ~200x110 box, in `body` color."""
        def ox(v): return x + v
        def oy(v): return y + v

        # Tail (polygon — approximates the curl)
        d.polygon([
            (ox(60),  oy(70)), (ox(40), oy(72)), (ox(24), oy(64)),
            (ox(16),  oy(40)), (ox(24), oy(22)), (ox(40), oy(30)),
            (ox(36),  oy(50)), (ox(54), oy(64)),
        ], fill=body)

        # Body
        d.ellipse([ox(45), oy(50), ox(155), oy(86)], fill=body)
        # Back leg + toes
        d.ellipse([ox(61), oy(73), ox(79), oy(99)], fill=body)
        for tx in (63, 70, 77):
            d.ellipse([ox(tx-3), oy(94), ox(tx+3), oy(102)], fill=body)
        # Front leg + toes
        d.ellipse([ox(126), oy(73), ox(144), oy(99)], fill=body)
        for tx in (128, 135, 142):
            d.ellipse([ox(tx-3), oy(94), ox(tx+3), oy(102)], fill=body)
        # Head
        d.ellipse([ox(132), oy(30), ox(188), oy(70)], fill=body)

        # Spots on the body — slightly darker than body color
        spot = tuple(max(0, c - 60) for c in body)
        d.ellipse([ox(82), oy(59), ox(88), oy(65)], fill=spot)
        d.ellipse([ox(102), oy(55), ox(108), oy(61)], fill=spot)
        d.ellipse([ox(122), oy(61), ox(128), oy(67)], fill=spot)

        # Eye
        d.ellipse([ox(166), oy(42), ox(178), oy(54)], fill=WHITE)
        d.ellipse([ox(170), oy(46), ox(176), oy(52)], fill=(0, 0, 0))

        # Mouth varies with mood
        # Bounds for the arc: a small box around (160-178, ~58)
        if mood == "happy":
            # Smile (∪) — bottom half of a small circle
            d.arc([ox(156), oy(54), ox(180), oy(68)], start=0, end=180,
                  fill=(0, 0, 0), width=2)
        elif mood == "neutral":
            d.line([(ox(158), oy(60)), (ox(178), oy(60))],
                   fill=(0, 0, 0), width=2)
        else:  # upset
            # Frown (∩) — top half of a small circle, sitting low
            d.arc([ox(156), oy(60), ox(180), oy(74)], start=180, end=360,
                  fill=(0, 0, 0), width=2)

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