"""
care_button.py
Physical push-button that logs the first item in [care].care_items
when pressed.

Uses a polling thread (not GPIO.add_event_detect) because event detection
in RPi.GPIO has been unreliable on newer Pi OS kernels — polling at 50 ms
is cheap and works everywhere.

Wiring (default, with pull_up = true):
    Button between GPIO pin and GND. Internal pull-up keeps the line HIGH;
    pressing pulls it LOW.
"""

import logging
import threading
import time

try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    _GPIO_AVAILABLE = True
except ImportError:
    _GPIO_AVAILABLE = False

logger = logging.getLogger(__name__)


class CareButton:
    def __init__(self, config, data_logger):
        self.config      = config
        self.data_logger = data_logger
        self.pin         = None
        self.pull_up     = True
        self.debounce_ms = 300
        self._last_press = 0.0
        self._running    = False
        self._thread     = None

        if not config.has_section("care_button"):
            logger.info("Care button: no [care_button] section in config — skipping.")
            return
        cfg = config["care_button"]
        if not cfg.getboolean("enabled", fallback=False):
            logger.info("Care button disabled in config (enabled = false).")
            return
        if not _GPIO_AVAILABLE:
            logger.warning("Care button enabled but RPi.GPIO not available.")
            return

        pin         = cfg.getint("gpio_pin",    fallback=23)
        pull_up     = cfg.getboolean("pull_up", fallback=True)
        debounce_ms = cfg.getint("debounce_ms", fallback=300)

        try:
            GPIO.setup(pin, GPIO.IN,
                       pull_up_down=GPIO.PUD_UP if pull_up else GPIO.PUD_DOWN)
        except Exception as e:
            logger.error("Care button: failed to set up GPIO %d: %s", pin, e)
            return

        self.pin         = pin
        self.pull_up     = pull_up
        self.debounce_ms = debounce_ms
        self._running    = True
        self._thread     = threading.Thread(target=self._watch_loop,
                                            daemon=True, name="care-button")
        self._thread.start()
        logger.info("Care button on GPIO %d (pull-%s, debounce %dms) watching.",
                    pin, "up" if pull_up else "down", debounce_ms)

    def _watch_loop(self):
        # With pull-up:  pressed = LOW (input == 0). Idle = HIGH (input == 1).
        # With pull-down: pressed = HIGH. Idle = LOW.
        idle_state    = 1 if self.pull_up else 0
        pressed_state = 0 if self.pull_up else 1
        last = idle_state
        while self._running:
            try:
                state = GPIO.input(self.pin)
            except Exception as e:
                logger.error("Care button: GPIO read failed: %s", e)
                time.sleep(1.0)
                continue
            # Edge: idle → pressed
            if last == idle_state and state == pressed_state:
                self._fire()
            last = state
            time.sleep(0.05)  # 50 ms poll

    def _fire(self):
        now = time.monotonic()
        if now - self._last_press < self.debounce_ms / 1000.0:
            return
        self._last_press = now
        item = self._first_care_item()
        if not item:
            logger.warning("Care button pressed but no care_items configured.")
            return
        try:
            self.data_logger.log_system_event("care", f"{item} (button)")
            logger.info("Care button pressed → logged '%s'", item)
        except Exception as e:
            logger.exception("Care button logging failed: %s", e)

    def _first_care_item(self) -> str:
        items = self.config.get("care", "care_items", fallback="Cleaning")
        for raw in items.split(","):
            s = raw.strip()
            if s:
                return s
        return ""

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
