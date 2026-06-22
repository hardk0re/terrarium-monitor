"""
homeassistant.py
Tiny Home Assistant REST client. Used by HA-backed relays (e.g. the mister
running through a switch.* entity) so we don't need GPIO/relays directly.

Credentials live in [general].ha_base_url and [general].ha_token in config.
Create the token in HA → Profile → Long-Lived Access Tokens.
"""

import logging
import requests

logger = logging.getLogger(__name__)


class HomeAssistantClient:
    def __init__(self, base_url: str, token: str):
        url = (base_url or "").rstrip("/")
        # Accept both "http://host:8123" and "http://host:8123/api"
        if url.endswith("/api"):
            url = url[:-4]
        self.base_url = url
        self.token    = (token or "").strip()
        self.enabled  = bool(self.base_url and self.token)
        if self.enabled:
            logger.info("Home Assistant client → %s", self.base_url)
        else:
            logger.info("Home Assistant client not configured "
                        "(missing ha_base_url or ha_token).")

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type":  "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/{path.lstrip('/')}"

    def call_service(self, domain: str, service: str, data: dict) -> bool:
        """Invoke a service like switch.turn_on with {entity_id: ...}."""
        if not self.enabled:
            logger.warning("HA not configured — skipping %s.%s call.", domain, service)
            return False
        try:
            r = requests.post(self._url(f"services/{domain}/{service}"),
                              headers=self._headers(), json=data, timeout=8)
            r.raise_for_status()
            return True
        except Exception as e:
            logger.error("HA call_service(%s.%s) failed: %s", domain, service, e)
            return False

    def get_state(self, entity_id: str) -> dict | None:
        if not self.enabled:
            return None
        try:
            r = requests.get(self._url(f"states/{entity_id}"),
                             headers=self._headers(), timeout=8)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error("HA get_state(%s) failed: %s", entity_id, e)
            return None
