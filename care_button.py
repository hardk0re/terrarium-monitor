"""
care_button.py
Physical push-button(s) that log a configured message under a configured
category when pressed. Up to three buttons can be wired via the
[care_button_1], [care_button_2], [care_button_3] config sections.

A polling thread (50 ms) is used rather than GPIO.add_event_detect because
event detection has been unreliable on newer Pi OS kernels.

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
    def __init__(self, config, data_logger, section: str = "care_button",
                 display=None, mister=None):
        self.config      = config
        self.data_logger = data_logger
        self.display     = display
        self.mister      = mister
        self.section     = section
        self.pin         = None
        self.pull_up     = True
        self.debounce_ms = 300
        self.category    = "care"
        self.message     = ""
        self._last_press = 0.0
        self._running    = False
        self._thread     = None

        if not config.has_section(section):
            return
        cfg = config[section]
        if not cfg.getboolean("enabled", fallback=False):
            logger.info("%s disabled in config.", section)
            return
        if not _GPIO_AVAILABLE:
            logger.warning("%s enabled but RPi.GPIO not available.", section)
            return

        pin         = cfg.getint("gpio_pin",    fallback=13)
        pull_up     = cfg.getboolean("pull_up", fallback=True)
        debounce_ms = cfg.getint("debounce_ms", fallback=300)
        category    = (cfg.get("category", fallback="care").strip() or "care")
        message     = cfg.get("message", fallback="").strip()

        # Back-compat: if no explicit message, fall back to the first item in
        # [care].care_items (this is what the original single-button setup did).
        if not message:
            items = config.get("care", "care_items", fallback="Cleaning")
            for raw in items.split(","):
                s = raw.strip()
                if s:
                    message = s
                    break
        if not message:
            message = "Button press"

        try:
            GPIO.setup(pin, GPIO.IN,
                       pull_up_down=GPIO.PUD_UP if pull_up else GPIO.PUD_DOWN)
        except Exception as e:
            logger.error("%s: failed to set up GPIO %d: %s", section, pin, e)
            return

        self.pin         = pin
        self.pull_up     = pull_up
        self.debounce_ms = debounce_ms
        self.category    = category
        self.message     = message
        self._running    = True
        self._thread     = threading.Thread(target=self._watch_loop,
                                            daemon=True, name=section)
        self._thread.start()
        logger.info("%s on GPIO %d (pull-%s) → log '%s' as category '%s'.",
                    section, pin, "up" if pull_up else "down",
                    message, category)

    def _watch_loop(self):
        idle_state    = 1 if self.pull_up else 0
        pressed_state = 0 if self.pull_up else 1
        last = idle_state
        while self._running:
            try:
                state = GPIO.input(self.pin)
            except Exception as e:
                logger.error("%s: GPIO read failed: %s", self.section, e)
                time.sleep(1.0)
                continue
            if last == idle_state and state == pressed_state:
                self._fire()
            last = state
            time.sleep(0.05)

    def _fire(self):
        now = time.monotonic()
        if now - self._last_press < self.debounce_ms / 1000.0:
            return
        self._last_press = now
        try:
            self.data_logger.log_system_event(self.category,
                                              f"{self.message} (button)")
            logger.info("%s pressed → logged '%s' as '%s'",
                        self.section, self.message, self.category)
        except Exception as e:
            logger.exception("%s logging failed: %s", self.section, e)

        # If this is a care-category button whose message matches the
        # configured mister_trigger_items list, also fire the mister —
        # same behavior as clicking that item in the dashboard care log.
        if self.mister and self.category == "care":
            try:
                raw = self.config.get("care", "mister_trigger_items", fallback="")
                triggers = [t.strip().lower() for t in raw.split(",") if t.strip()]
                if self.message.lower() in triggers:
                    if not getattr(self.mister, "button_enabled", True):
                        logger.info("%s: mister button disabled in config — skipping.",
                                    self.section)
                    elif self.mister.trigger(reason=f"care button: {self.message}"):
                        logger.info("%s: fired mister.", self.section)
                    else:
                        logger.info("%s: mister trigger blocked "
                                    "(cooldown / 24h cap / already on).",
                                    self.section)
            except Exception as e:
                logger.warning("%s: mister trigger check failed: %s", self.section, e)

        # Flash a confirmation on the OLED. Separate try so a display failure
        # doesn't mask a successful log entry.
        if self.display:
            try:
                self.display.flash(self.category, self.message)
            except Exception as e:
                logger.warning("%s flash failed: %s", self.section, e)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
