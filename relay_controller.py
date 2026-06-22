"""
relay_controller.py
Controls GPIO relays for the mister and fan with run-time limits,
monitor delays, and 24-hour run-count caps.
"""

import time
import logging
import threading
import configparser
from datetime import datetime

try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    _GPIO_AVAILABLE = True
except ImportError:
    _GPIO_AVAILABLE = False
    logging.warning("RPi.GPIO not available – running in simulation mode.")

logger = logging.getLogger(__name__)


class RelayController:
    """
    Manages a single GPIO relay with:
      - configurable ON duration
      - post-run monitor delay before re-evaluating
      - 24-hour run count cap
    """

    def __init__(self, name: str, gpio_pin: int, active_low: bool,
                 runtime_minutes: float, monitor_delay_minutes: float,
                 max_runs_per_24h: int, data_logger=None):
        self.name                = name
        self.gpio_pin            = gpio_pin
        self.active_low          = active_low
        self.runtime_minutes     = runtime_minutes
        self.monitor_delay_min   = monitor_delay_minutes
        self.max_runs_per_24h    = max_runs_per_24h
        self.data_logger         = data_logger

        self._is_on              = False
        self._lock               = threading.Lock()
        self._cooldown_until     = 0.0   # epoch seconds – don't re-trigger before this

        if _GPIO_AVAILABLE:
            GPIO.setup(self.gpio_pin, GPIO.OUT)
            self._write(False)

    # ------------------------------------------------------------------
    # Internal GPIO helpers
    # ------------------------------------------------------------------

    def _write(self, state: bool):
        """Drive the GPIO pin, honouring active_low wiring."""
        if not _GPIO_AVAILABLE:
            logger.info("[SIM] GPIO %d → %s", self.gpio_pin, "ON" if state else "OFF")
            return
        GPIO.output(self.gpio_pin, not state if self.active_low else state)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def in_cooldown(self) -> bool:
        return time.monotonic() < self._cooldown_until

    def _run_count_24h(self) -> int:
        if self.data_logger:
            return self.data_logger.get_relay_run_count(self.name, hours=24)
        return 0

    def trigger(self, reason: str = "") -> bool:
        """
        Attempt to start a relay cycle.
        - runtime_minutes > 0: timed cycle, then cooldown, capped at max_runs_per_24h.
        - runtime_minutes <= 0: continuous mode — turn on and leave on. The
          caller is responsible for calling force_off() when conditions clear.
          The 24h run cap is skipped in continuous mode since there are no
          discrete "runs" to count.
        Returns True if the relay was turned on, False if blocked.
        """
        continuous = self.runtime_minutes <= 0
        with self._lock:
            if self._is_on:
                logger.debug("%s already running.", self.name)
                return False

            if self.in_cooldown:
                remaining = self._cooldown_until - time.monotonic()
                logger.info("%s in cooldown – %.1f min remaining.", self.name, remaining / 60)
                return False

            if not continuous and self.max_runs_per_24h > 0:
                runs = self._run_count_24h()
                if runs >= self.max_runs_per_24h:
                    logger.warning("%s hit 24h run cap (%d/%d).",
                                   self.name, runs, self.max_runs_per_24h)
                    return False

            self._is_on = True
            self._write(True)
            if self.data_logger:
                self.data_logger.log_relay_event(self.name, "ON", reason)
            mode_label = "continuous" if continuous else f"runtime {self.runtime_minutes:.1f} min"
            logger.info("%s ON – %s | reason: %s", self.name, mode_label, reason)

        if not continuous:
            t = threading.Thread(target=self._auto_off, daemon=True)
            t.start()
        return True

    def force_off(self):
        """Immediately turn the relay off (e.g. on shutdown)."""
        with self._lock:
            self._write(False)
            if self._is_on:
                self._is_on = False
                if self.data_logger:
                    self.data_logger.log_relay_event(self.name, "OFF", "forced off")
                logger.info("%s force OFF.", self.name)

    # ------------------------------------------------------------------
    # Background cycle
    # ------------------------------------------------------------------

    def _auto_off(self):
        """Run for runtime_minutes, then start the monitor delay cooldown."""
        time.sleep(self.runtime_minutes * 60)
        with self._lock:
            self._write(False)
            self._is_on = False
            cooldown_sec = self.monitor_delay_min * 60
            self._cooldown_until = time.monotonic() + cooldown_sec
            if self.data_logger:
                self.data_logger.log_relay_event(self.name, "OFF",
                                                 f"auto off after {self.runtime_minutes} min")
        logger.info("%s OFF – monitor delay %.1f min before re-check.",
                    self.name, self.monitor_delay_min)


# ======================================================================
# HARelay — mimics RelayController's public API but drives a Home Assistant
# entity (switch.*) instead of a GPIO pin. Runtime/cooldown are tracked in
# SECONDS rather than minutes for finer control (misting cycles are short).
# ======================================================================

class HARelay:
    def __init__(self, name: str, ha_client, entity_id: str,
                 runtime_seconds: float, monitor_delay_seconds: float,
                 max_runs_per_24h: int, data_logger=None,
                 auto_enabled: bool = True, button_enabled: bool = True):
        self.name                  = name
        self.ha_client             = ha_client
        self.entity_id             = entity_id
        self.runtime_seconds       = runtime_seconds
        self.monitor_delay_seconds = monitor_delay_seconds
        self.max_runs_per_24h      = max_runs_per_24h
        self.data_logger           = data_logger
        self.auto_enabled          = auto_enabled
        self.button_enabled        = button_enabled

        self._is_on            = False
        self._lock             = threading.Lock()
        self._cooldown_until   = 0.0

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def in_cooldown(self) -> bool:
        return time.monotonic() < self._cooldown_until

    def _run_count_24h(self) -> int:
        if self.data_logger:
            return self.data_logger.get_relay_run_count(self.name, hours=24)
        return 0

    def _domain(self) -> str:
        # "switch.terrarium_mister" → "switch". Fall back to "switch" if no dot.
        return self.entity_id.split(".", 1)[0] if "." in self.entity_id else "switch"

    def _send(self, state: bool) -> bool:
        service = "turn_on" if state else "turn_off"
        return self.ha_client.call_service(
            self._domain(), service, {"entity_id": self.entity_id}
        )

    def trigger(self, reason: str = "") -> bool:
        """Run for runtime_seconds, then auto-off + cooldown for
        monitor_delay_seconds. runtime_seconds <= 0 means continuous (no timer)."""
        continuous = self.runtime_seconds <= 0
        with self._lock:
            if self._is_on:
                logger.debug("%s already running.", self.name)
                return False
            if self.in_cooldown:
                remaining = self._cooldown_until - time.monotonic()
                logger.info("%s in cooldown – %.0fs remaining.", self.name, remaining)
                return False
            if not continuous and self.max_runs_per_24h > 0:
                runs = self._run_count_24h()
                if runs >= self.max_runs_per_24h:
                    logger.warning("%s hit 24h run cap (%d/%d).",
                                   self.name, runs, self.max_runs_per_24h)
                    return False
            if not self._send(True):
                return False
            self._is_on = True
            if self.data_logger:
                self.data_logger.log_relay_event(self.name, "ON", reason)
            mode_label = "continuous" if continuous else f"runtime {self.runtime_seconds:.0f}s"
            logger.info("%s ON – %s | reason: %s", self.name, mode_label, reason)

        if not continuous:
            threading.Thread(target=self._auto_off, daemon=True).start()
        return True

    def force_off(self):
        with self._lock:
            ok = self._send(False)
            if self._is_on:
                self._is_on = False
                if self.data_logger:
                    self.data_logger.log_relay_event(self.name, "OFF", "forced off")
                logger.info("%s force OFF%s.", self.name,
                            "" if ok else " (HA call failed; state cleared anyway)")

    def _auto_off(self):
        time.sleep(self.runtime_seconds)
        with self._lock:
            self._send(False)
            self._is_on = False
            self._cooldown_until = time.monotonic() + self.monitor_delay_seconds
            if self.data_logger:
                self.data_logger.log_relay_event(self.name, "OFF",
                                                 f"auto off after {self.runtime_seconds:.0f}s")
        logger.info("%s OFF – monitor delay %.0fs before re-check.",
                    self.name, self.monitor_delay_seconds)


# ======================================================================
# Factory helpers
# ======================================================================

def build_mister(config: configparser.ConfigParser, data_logger=None,
                 ha_client=None) -> "HARelay | None":
    """Mister now runs via Home Assistant rather than GPIO."""
    cfg = config["mister"]
    auto   = cfg.getboolean("enabled",        fallback=True)
    button = cfg.getboolean("button_enabled", fallback=True)
    if not auto and not button:
        return None
    if not ha_client or not ha_client.enabled:
        logger.error("Mister needs Home Assistant — set [general].ha_base_url + ha_token.")
        return None
    entity_id = cfg.get("ha_entity_id", fallback="").strip()
    if not entity_id:
        logger.error("Mister: [mister].ha_entity_id is missing.")
        return None
    return HARelay(
        name                  = "Mister",
        ha_client             = ha_client,
        entity_id             = entity_id,
        runtime_seconds       = cfg.getfloat("mister_runtime_seconds",        fallback=600),
        monitor_delay_seconds = cfg.getfloat("mister_monitor_delay_seconds",  fallback=900),
        max_runs_per_24h      = cfg.getint  ("mister_max_runs_per_24h",       fallback=6),
        data_logger           = data_logger,
        auto_enabled          = auto,
        button_enabled        = button,
    )


def build_fan(config: configparser.ConfigParser, data_logger=None) -> RelayController | None:
    cfg = config["fan"]
    if not cfg.getboolean("enabled", fallback=True):
        return None
    return RelayController(
        name                  = "Fan",
        gpio_pin              = cfg.getint("gpio_pin"),
        active_low            = cfg.getboolean("relay_active_low", fallback=True),
        runtime_minutes       = cfg.getfloat("fan_runtime_minutes", fallback=10),
        monitor_delay_minutes = cfg.getfloat("fan_monitor_delay_minutes", fallback=15),
        max_runs_per_24h      = cfg.getint("fan_max_runs_per_24h", fallback=12),
        data_logger           = data_logger,
    )


def cleanup_gpio():
    if _GPIO_AVAILABLE:
        GPIO.cleanup()
