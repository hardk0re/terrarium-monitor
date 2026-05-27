"""
weather_manager.py
Fetches outdoor temperature and weather data from OpenWeatherMap.
Stores readings in the SQLite database for 24h history display.
"""

import logging
import threading
import time
import requests
import configparser
from datetime import datetime

logger = logging.getLogger(__name__)

OWM_URL = "https://api.openweathermap.org/data/2.5/weather"


class WeatherManager:
    def __init__(self, config: configparser.ConfigParser, data_logger=None):
        self.config      = config
        self.data_logger = data_logger
        self._running    = False
        self._lock       = threading.Lock()
        self._latest     = None   # last WeatherReading

        if not config.has_section("weather"):
            self.enabled = False
            return

        cfg = config["weather"]
        self.enabled  = cfg.getboolean("enabled", fallback=True)
        if not self.enabled:
            return

        self.api_key       = cfg.get("api_key", "")
        self.lat           = cfg.getfloat("latitude",  fallback=0.0)
        self.lon           = cfg.getfloat("longitude", fallback=0.0)
        self.poll_interval = cfg.getint("poll_interval_seconds", fallback=600)
        self.temp_unit     = cfg.get("temp_unit",
                             config.get("display", "temp_unit", fallback="F")).upper()

        if not self.api_key or self.api_key == "your_api_key_here":
            logger.warning("Weather: no API key configured – weather disabled.")
            self.enabled = False
            return

        logger.info("Weather manager ready. Polling every %ds.", self.poll_interval)

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def fetch(self) -> dict | None:
        """Fetch current weather from OWM. Returns parsed dict or None."""
        try:
            r = requests.get(OWM_URL, params={
                "lat":   self.lat,
                "lon":   self.lon,
                "appid": self.api_key,
                "units": "metric",   # always fetch in C, convert in display
            }, timeout=10)
            if r.status_code != 200:
                logger.warning("Weather API returned %d: %s", r.status_code, r.text[:200])
                return None

            d = r.json()
            reading = {
                "ts":          datetime.utcnow().isoformat(),
                "temp_c":      d["main"]["temp"],
                "feels_like_c":d["main"]["feels_like"],
                "humidity":    d["main"]["humidity"],
                "description": d["weather"][0]["description"].title(),
                "icon":        d["weather"][0]["icon"],
                "wind_speed":  d["wind"]["speed"],       # m/s
                "city":        d.get("name", ""),
            }

            with self._lock:
                self._latest = reading

            if self.data_logger:
                self.data_logger.log_weather(reading)

            logger.debug("Weather: %.1f°C  %s  humidity %d%%",
                         reading["temp_c"], reading["description"], reading["humidity"])
            return reading

        except Exception as e:
            logger.error("Weather fetch failed: %s", e)
            return None

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def start(self):
        if not self.enabled:
            return
        self._running = True
        # Fetch immediately on start
        threading.Thread(target=self._loop, daemon=True, name="weather").start()
        logger.info("Weather loop started.")

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            self.fetch()
            for _ in range(self.poll_interval * 2):
                if not self._running:
                    break
                time.sleep(0.5)

    # ------------------------------------------------------------------
    # Status / latest reading
    # ------------------------------------------------------------------

    def latest(self) -> dict | None:
        with self._lock:
            return dict(self._latest) if self._latest else None

    def _convert_temp(self, temp_c: float) -> float:
        return temp_c * 9/5 + 32 if self.temp_unit == "F" else temp_c

    def status(self) -> dict:
        r = self.latest()
        if not r:
            return {"enabled": self.enabled, "available": False}
        unit = "°F" if self.temp_unit == "F" else "°C"
        return {
            "enabled":      self.enabled,
            "available":    True,
            "city":         r["city"],
            "temp_display": f"{self._convert_temp(r['temp_c']):.1f}{unit}",
            "temp_c":       r["temp_c"],
            "feels_like_display": f"{self._convert_temp(r['feels_like_c']):.1f}{unit}",
            "humidity":     r["humidity"],
            "description":  r["description"],
            "icon_url":     f"https://openweathermap.org/img/wn/{r['icon']}@2x.png",
            "wind_speed":   r["wind_speed"],
            "ts_local":     r["ts"].replace("T", " ")[:16],
            "temp_unit":    self.temp_unit,
        }
