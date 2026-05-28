"""
pushover_manager.py
Sends push notifications via Pushover API.
Supports per-category alert toggles and a per-condition cooldown
so you don't get spammed when a threshold is repeatedly crossed.

Sign up at https://pushover.net — free for 30 days, then $5 one-time.
You need:
  - An application API token  (create at https://pushover.net/apps)
  - Your user key             (shown on your Pushover dashboard)
"""

import logging
import time
import requests
import configparser
from datetime import datetime

logger = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"

# Priority levels
PRIORITY_LOW    = -1
PRIORITY_NORMAL =  0
PRIORITY_HIGH   =  1


class PushoverManager:
    def __init__(self, config: configparser.ConfigParser):
        self.config  = config
        self._cooldowns: dict[str, float] = {}  # key → last sent epoch

        if not config.has_section("pushover"):
            self.enabled = False
            return

        cfg = config["pushover"]
        self.enabled   = cfg.getboolean("enabled", fallback=False)
        self.api_token = cfg.get("api_token", "")
        self.user_key  = cfg.get("user_key",  "")
        self.device    = cfg.get("device",    "")
        self.sound     = cfg.get("sound",     "pushover")
        self.cooldown_sec = cfg.getint("alert_cooldown_minutes", fallback=30) * 60

        if not self.api_token or self.api_token == "your_app_token_here":
            logger.warning("Pushover: no API token configured – notifications disabled.")
            self.enabled = False
            return
        if not self.user_key or self.user_key == "your_user_key_here":
            logger.warning("Pushover: no user key configured – notifications disabled.")
            self.enabled = False
            return

        logger.info("Pushover notifications enabled.")

    # ------------------------------------------------------------------
    # Core send
    # ------------------------------------------------------------------

    def send(self, title: str, message: str,
             priority: int = PRIORITY_NORMAL,
             sound: str = None) -> bool:
        """Send a push notification. Returns True on success."""
        if not self.enabled:
            return False
        try:
            payload = {
                "token":   self.api_token,
                "user":    self.user_key,
                "title":   title,
                "message": message,
                "sound":   sound or self.sound,
                "priority": priority,
            }
            if self.device:
                payload["device"] = self.device

            r = requests.post(PUSHOVER_URL, data=payload, timeout=10)
            if r.status_code == 200:
                logger.info("Pushover sent: %s – %s", title, message)
                return True
            else:
                logger.warning("Pushover failed %d: %s", r.status_code, r.text[:200])
                return False
        except Exception as e:
            logger.error("Pushover error: %s", e)
            return False

    # ------------------------------------------------------------------
    # Cooldown helper
    # ------------------------------------------------------------------

    def _cooled_down(self, key: str) -> bool:
        """Return True if enough time has passed since last alert for this key."""
        last = self._cooldowns.get(key, 0)
        if time.monotonic() - last >= self.cooldown_sec:
            self._cooldowns[key] = time.monotonic()
            return True
        return False

    def _cfg_bool(self, key: str, fallback: bool = False) -> bool:
        try:
            return self.config.getboolean("pushover", key, fallback=fallback)
        except Exception:
            return fallback

    # ------------------------------------------------------------------
    # Category alert methods
    # ------------------------------------------------------------------

    def alert_temp_high(self, sensor_name: str, temp_val: float, unit: str, threshold: float):
        if not self._cfg_bool("alert_temp_high"): return
        key = f"temp_high_{sensor_name}"
        if self._cooled_down(key):
            self.send(
                "🌡 High Temperature Alert",
                f"{sensor_name}: {temp_val:.1f}°{unit} exceeds {threshold:.1f}°{unit}",
                priority=PRIORITY_HIGH
            )

    def alert_temp_low(self, sensor_name: str, temp_val: float, unit: str, threshold: float):
        if not self._cfg_bool("alert_temp_low"): return
        key = f"temp_low_{sensor_name}"
        if self._cooled_down(key):
            self.send(
                "🥶 Low Temperature Alert",
                f"{sensor_name}: {temp_val:.1f}°{unit} below {threshold:.1f}°{unit}",
                priority=PRIORITY_HIGH
            )

    def alert_humidity_high(self, sensor_name: str, humidity: float, threshold: float):
        if not self._cfg_bool("alert_humidity_high"): return
        key = f"hum_high_{sensor_name}"
        if self._cooled_down(key):
            self.send(
                "💧 High Humidity Alert",
                f"{sensor_name}: {humidity:.1f}% exceeds {threshold:.1f}%",
            )

    def alert_humidity_low(self, sensor_name: str, humidity: float, threshold: float):
        if not self._cfg_bool("alert_humidity_low"): return
        key = f"hum_low_{sensor_name}"
        if self._cooled_down(key):
            self.send(
                "🏜 Low Humidity Alert",
                f"{sensor_name}: {humidity:.1f}% below {threshold:.1f}%",
                priority=PRIORITY_HIGH
            )

    def alert_relay(self, relay_name: str, action: str, reason: str = ""):
        key_map = {
            ("Mister", "ON"):  "alert_mister_on",
            ("Mister", "OFF"): "alert_mister_off",
            ("Fan",    "ON"):  "alert_fan_on",
            ("Fan",    "OFF"): "alert_fan_off",
        }
        cfg_key = key_map.get((relay_name, action))
        if not cfg_key or not self._cfg_bool(cfg_key): return
        key = f"relay_{relay_name}_{action}"
        if self._cooled_down(key):
            icons = {"Mister": "💦", "Fan": "🌀"}
            icon  = icons.get(relay_name, "⚡")
            self.send(
                f"{icon} {relay_name} {action}",
                reason or f"{relay_name} turned {action}",
            )

    def alert_light(self, plug_name: str, state: bool):
        if not self._cfg_bool("alert_lights"): return
        key = f"light_{plug_name}_{state}"
        if self._cooled_down(key):
            self.send(
                f"💡 {plug_name} {'ON' if state else 'OFF'}",
                f"{plug_name} turned {'on' if state else 'off'} on schedule.",
            )

    def alert_sensor_error(self, sensor_name: str):
        if not self._cfg_bool("alert_sensor_error"): return
        key = f"sensor_error_{sensor_name}"
        if self._cooled_down(key):
            self.send(
                "⚠️ Sensor Disconnected",
                f"Sensor '{sensor_name}' is not responding.",
                priority=PRIORITY_HIGH,
                sound="siren"
            )

    def alert_feeding(self, item: str, note: str = ""):
        if not self._cfg_bool("alert_feeding"): return
        if self._cooled_down("feeding"):
            msg = f"Fed: {item}"
            if note: msg += f" — {note}"
            self.send("🍽 Feeding Logged", msg, priority=PRIORITY_LOW)

    def alert_care(self, item: str, note: str = ""):
        if not self._cfg_bool("alert_care"): return
        if self._cooled_down("care"):
            msg = f"Care: {item}"
            if note: msg += f" — {note}"
            self.send("🩺 Care Logged", msg, priority=PRIORITY_LOW)

    def alert_weather(self, description: str, temp_val: float, unit: str):
        if not self._cfg_bool("alert_weather"): return
        if self._cooled_down("weather"):
            self.send(
                "🌤 Weather Update",
                f"Outdoor: {temp_val:.1f}°{unit}, {description}",
                priority=PRIORITY_LOW
            )

    def test(self) -> bool:
        """Send a test notification."""
        return self.send(
            "🦎 Terrarium Monitor",
            "Pushover notifications are working!",
            priority=PRIORITY_NORMAL
        )
