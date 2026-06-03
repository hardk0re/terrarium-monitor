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
# Factory helpers
# ======================================================================

def build_mister(config: configparser.ConfigParser, data_logger=None) -> RelayController | None:
    cfg = config["mister"]
    if not cfg.getboolean("enabled", fallback=True):
        return None
    return RelayController(
        name                  = "Mister",
        gpio_pin              = cfg.getint("gpio_pin"),
        active_low            = cfg.getboolean("relay_active_low", fallback=True),
        runtime_minutes       = cfg.getfloat("mister_runtime_minutes", fallback=10),
        monitor_delay_minutes = cfg.getfloat("mister_monitor_delay_minutes", fallback=15),
        max_runs_per_24h      = cfg.getint("mister_max_runs_per_24h", fallback=6),
        data_logger           = data_logger,
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
