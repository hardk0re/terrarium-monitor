"""
tapo_controller.py
Controls TAPO WiFi smart plugs via local UDP broadcast (no cloud, no auth).
Uses the PyP100 / tplink-smarthome local protocol (port 9999).

Compatible plugs: P100, P105, P110 and similar TP-Link Tapo devices
that support the unauthenticated local UDP command on firmware < 1.2.x,
OR the newer tplink_smarthome_protocol for updated firmware.

Install: pip install PyP100
"""

import logging
import socket
import json
import configparser
from datetime import datetime, time as dtime
from threading import Thread
import time

logger = logging.getLogger(__name__)

# Try PyP100 first, fall back to raw UDP
try:
    from PyP100 import PyP100
    _USE_PYP100 = True
    logger.info("Using PyP100 library for TAPO control.")
except ImportError:
    _USE_PYP100 = False
    logger.warning("PyP100 not found – falling back to raw UDP broadcast.")


# ======================================================================
# Low-level raw UDP helper (no-auth protocol, older firmware)
# ======================================================================

_XOR_KEY = 0xAB

def _xor_encrypt(data: bytes) -> bytes:
    key = _XOR_KEY
    out = []
    for b in data:
        enc = b ^ key
        key = enc
        out.append(enc)
    return bytes(out)

def _build_packet(payload: dict) -> bytes:
    raw = json.dumps(payload).encode()
    encrypted = _xor_encrypt(raw)
    # 4-byte big-endian length header
    return len(encrypted).to_bytes(4, "big") + encrypted

def _raw_send(ip: str, payload: dict, port: int = 9999, timeout: float = 3.0) -> dict | None:
    try:
        pkt = _build_packet(payload)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((ip, port))
            s.sendall(pkt)
            resp = s.recv(4096)
        if len(resp) < 4:
            return None
        body = resp[4:]           # strip length header
        key = _XOR_KEY
        plain = []
        for b in body:
            plain.append(b ^ key)
            key = b
        return json.loads(bytes(plain).decode())
    except Exception as e:
        logger.error("Raw UDP/TCP error talking to %s: %s", ip, e)
        return None


# ======================================================================
# TapoPlug – single plug abstraction
# ======================================================================

class TapoPlug:
    def __init__(self, name: str, ip: str, on_time: dtime, off_time: dtime,
                 data_logger=None):
        self.name       = name
        self.ip         = ip
        self.on_time    = on_time
        self.off_time   = off_time
        self.data_logger = data_logger
        self._state     = None   # True = on, False = off, None = unknown

        if _USE_PYP100:
            try:
                self._device = PyP100.P100(ip, "", "")   # no credentials needed
                self._device.handshake()
            except Exception as e:
                logger.warning("PyP100 handshake failed for %s (%s): %s", name, ip, e)
                self._device = None
        else:
            self._device = None

    # ------------------------------------------------------------------

    def turn_on(self) -> bool:
        logger.info("Turning ON: %s (%s)", self.name, self.ip)
        ok = False
        if self._device:
            try:
                self._device.turnOn()
                ok = True
            except Exception as e:
                logger.error("PyP100 turn_on failed for %s: %s", self.name, e)
        if not ok:
            payload = {"system": {"set_relay_state": {"state": 1}}}
            ok = bool(_raw_send(self.ip, payload))
        if ok:
            self._state = True
            if self.data_logger:
                self.data_logger.log_system_event("light", f"{self.name} turned ON")
        return ok

    def turn_off(self) -> bool:
        logger.info("Turning OFF: %s (%s)", self.name, self.ip)
        ok = False
        if self._device:
            try:
                self._device.turnOff()
                ok = True
            except Exception as e:
                logger.error("PyP100 turn_off failed for %s: %s", self.name, e)
        if not ok:
            payload = {"system": {"set_relay_state": {"state": 0}}}
            ok = bool(_raw_send(self.ip, payload))
        if ok:
            self._state = False
            if self.data_logger:
                self.data_logger.log_system_event("light", f"{self.name} turned OFF")
        return ok

    def get_info(self) -> dict | None:
        if self._device:
            try:
                return self._device.getDeviceInfo()
            except Exception:
                pass
        payload = {"system": {"get_sysinfo": {}}}
        return _raw_send(self.ip, payload)

    @property
    def current_state(self) -> bool | None:
        return self._state

    def should_be_on(self, now: dtime | None = None) -> bool:
        """Returns True if current time falls within the on/off schedule."""
        now = now or datetime.now().time().replace(second=0, microsecond=0)
        if self.on_time <= self.off_time:
            return self.on_time <= now < self.off_time
        else:
            # Overnight schedule e.g. 22:00 → 06:00
            return now >= self.on_time or now < self.off_time


# ======================================================================
# LightingController – manages all plugs + schedule loop
# ======================================================================

class LightingController:
    def __init__(self, config: configparser.ConfigParser, data_logger=None):
        self.plugs: list[TapoPlug] = []
        self._running = False
        self._dl      = data_logger

        if not config.getboolean("lighting", "enabled", fallback=True):
            logger.info("Lighting control disabled.")
            return

        for section in config.sections():
            if not section.lower().startswith("tapo_plug_"):
                continue
            cfg = config[section]
            if not cfg.getboolean("enabled", fallback=True):
                continue
            try:
                on_h,  on_m  = map(int, cfg["on_time"].split(":"))
                off_h, off_m = map(int, cfg["off_time"].split(":"))
                plug = TapoPlug(
                    name        = cfg.get("name", section),
                    ip          = cfg["ip_address"],
                    on_time     = dtime(on_h, on_m),
                    off_time    = dtime(off_h, off_m),
                    data_logger = self._dl,
                )
                self.plugs.append(plug)
                logger.info("Lighting plug '%s' at %s  ON:%02d:%02d  OFF:%02d:%02d",
                            plug.name, plug.ip, on_h, on_m, off_h, off_m)
            except Exception as e:
                logger.error("Failed to configure plug [%s]: %s", section, e)

    def start_schedule_loop(self):
        """Start a background thread that checks schedule every 60 s."""
        if not self.plugs:
            return
        self._running = True
        Thread(target=self._loop, daemon=True, name="lighting-sched").start()

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            self._tick()
            time.sleep(60)

    def _tick(self):
        now = datetime.now().time().replace(second=0, microsecond=0)
        for plug in self.plugs:
            desired = plug.should_be_on(now)
            if desired and plug.current_state is not True:
                plug.turn_on()
            elif not desired and plug.current_state is not False:
                plug.turn_off()

    def force_all_off(self):
        for plug in self.plugs:
            plug.turn_off()

    def status(self) -> list[dict]:
        return [
            {
                "name": p.name,
                "ip": p.ip,
                "on_time": p.on_time.strftime("%H:%M"),
                "off_time": p.off_time.strftime("%H:%M"),
                "state": p.current_state,
                "should_be_on": p.should_be_on(),
            }
            for p in self.plugs
        ]