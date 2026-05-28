"""
pushover_notifier.py
Thin wrapper around the Pushover messaging API.

Usage:
    notifier = PushoverNotifier(config)
    notifier.send("hello")                           # ad-hoc message
    notifier.notify_event("care", "Logged Misting")  # category-gated
    notifier.notify_mood_change("happy", "upset", "Top humidity 22%")
    ok, msg = notifier.test()                        # invoked by /api/pushover/test

All network failures are swallowed and logged — Pushover going down should
never crash the monitor.
"""

import logging
import requests

logger = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
_REQUEST_TIMEOUT = 8


class PushoverNotifier:
    def __init__(self, config):
        self.config = config

    def reload(self, config=None):
        """Pick up new credentials after a config edit."""
        if config is not None:
            self.config = config

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.config.getboolean("pushover", "enabled", fallback=False)

    @property
    def api_token(self) -> str:
        return self.config.get("pushover", "api_token", fallback="").strip()

    @property
    def user_key(self) -> str:
        return self.config.get("pushover", "user_key", fallback="").strip()

    @property
    def device(self) -> str:
        return self.config.get("pushover", "device", fallback="").strip()

    @property
    def default_sound(self) -> str:
        return self.config.get("pushover", "sound", fallback="pushover").strip()

    def is_configured(self) -> bool:
        return bool(self.enabled and self.api_token and self.user_key)

    def category_enabled(self, category: str) -> bool:
        return self.config.getboolean(
            "pushover", f"notify_{category}", fallback=False
        )

    def mood_change_enabled(self) -> bool:
        return self.config.getboolean(
            "pushover", "notify_gecko_mood_change", fallback=False
        )

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    def send(self, message: str, title: str = None,
             priority: int = 0, sound: str = None, url: str = None) -> bool:
        if not self.is_configured():
            logger.debug("Pushover not configured; skipping send.")
            return False
        data = {
            "token":    self.api_token,
            "user":     self.user_key,
            "message":  message,
            "priority": priority,
        }
        if title:                       data["title"]  = title
        if self.device:                 data["device"] = self.device
        if sound or self.default_sound: data["sound"]  = sound or self.default_sound
        if url:                         data["url"]    = url
        try:
            r = requests.post(PUSHOVER_URL, data=data, timeout=_REQUEST_TIMEOUT)
            if r.status_code == 200:
                logger.info("Pushover sent: %s", title or message[:60])
                return True
            logger.warning("Pushover send failed (%d): %s",
                           r.status_code, r.text[:200])
        except Exception as e:
            logger.warning("Pushover send error: %s", e)
        return False

    # ------------------------------------------------------------------
    # Convenience dispatchers
    # ------------------------------------------------------------------

    def notify_event(self, category: str, message: str) -> None:
        """Called by DataLogger after a system event is recorded."""
        if not self.is_configured() or not self.category_enabled(category):
            return
        title = f"Terrarium · {category}"
        # Errors get the louder/longer sound
        sound = "siren" if category == "error" else None
        priority = 1 if category == "error" else 0
        self.send(message, title=title, priority=priority, sound=sound)

    def notify_mood_change(self, old_mood: str, new_mood: str, summary: str) -> None:
        if not self.is_configured() or not self.mood_change_enabled():
            return
        icon = {"happy": "🟢", "neutral": "🟡", "upset": "🔴"}.get(new_mood, "🦎")
        title = f"{icon} Gecko mood: {old_mood} → {new_mood}"
        # Bump priority + use a louder sound when the gecko becomes upset
        priority = 1 if new_mood == "upset" else 0
        sound = "falling" if new_mood == "upset" else None
        self.send(summary or new_mood, title=title,
                  priority=priority, sound=sound)

    def test(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "Pushover is disabled in config (enabled = false)."
        if not self.api_token or not self.user_key:
            return False, "Missing api_token or user_key in [pushover]."
        ok = self.send(
            "Test notification from your Terrarium Monitor 🦎",
            title="Terrarium · Test",
        )
        return (ok, "Test sent." if ok else "Pushover API returned an error.")
