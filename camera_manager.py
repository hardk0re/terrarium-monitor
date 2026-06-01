"""
camera_manager.py
TAPO camera integration via raw ONVIF SOAP calls.
No onvif-zeep dependency — uses requests + minimal XML parsing.

Dependencies:
  pip install requests opencv-python-headless
"""

import logging
import threading
import time
import re
import hashlib
import base64
import os
import shutil
import requests
import requests.auth
import configparser
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Tighten FFMPEG defaults BEFORE cv2 is imported. Without this, OpenCV's
# RTSP backend blocks for 30 s when the camera is unreachable. stimeout is
# microseconds (5 s) and rtsp_transport=tcp tends to be more reliable than
# UDP for IP cameras. Override OPENCV_FFMPEG_CAPTURE_OPTIONS in the
# environment if you need different values.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|stimeout;5000000",
)

try:
    import cv2
    _CV2_OK = True
except ImportError:
    _CV2_OK = False
    logger.warning("opencv-python-headless not installed – RTSP frame grab unavailable.")

# ── WS-Security + SOAP ───────────────────────────────────────────────

def _wsse_header(user: str, pwd: str) -> str:
    """Build a WS-Security UsernameToken header (PasswordDigest, ONVIF standard)."""
    nonce_raw  = os.urandom(20)
    nonce_b64  = base64.b64encode(nonce_raw).decode()
    created    = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest_raw = hashlib.sha1(nonce_raw + created.encode() + pwd.encode()).digest()
    digest_b64 = base64.b64encode(digest_raw).decode()
    return f"""<s:Header>
  <wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
                 xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
    <wsse:UsernameToken>
      <wsse:Username>{user}</wsse:Username>
      <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd#PasswordDigest">{digest_b64}</wsse:Password>
      <wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd#Base64Binary">{nonce_b64}</wsse:Nonce>
      <wsu:Created>{created}</wsu:Created>
    </wsse:UsernameToken>
  </wsse:Security>
</s:Header>"""

_SOAP_ENV = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
            xmlns:trt="http://www.onvif.org/ver10/media/wsdl">
  {header}
  <s:Body>{body}</s:Body>
</s:Envelope>"""

_GET_PROFILES   = "<trt:GetProfiles/>"
_GET_STREAM_URI = """<trt:GetStreamUri>
  <trt:StreamSetup>
    <tt:Stream xmlns:tt="http://www.onvif.org/ver10/schema">RTP-Unicast</tt:Stream>
    <tt:Transport xmlns:tt="http://www.onvif.org/ver10/schema">
      <tt:Protocol>RTSP</tt:Protocol>
    </tt:Transport>
  </trt:StreamSetup>
  <trt:ProfileToken>{token}</trt:ProfileToken>
</trt:GetStreamUri>"""
_GET_SNAP_URI = """<trt:GetSnapshotUri>
  <trt:ProfileToken>{token}</trt:ProfileToken>
</trt:GetSnapshotUri>"""


def _soap(url: str, body: str, user: str, pwd: str, timeout: int = 5) -> str | None:
    payload = _SOAP_ENV.format(header=_wsse_header(user, pwd), body=body)
    headers = {"Content-Type": "application/soap+xml; charset=utf-8"}
    try:
        r = requests.post(url, data=payload, headers=headers, timeout=timeout)
        if r.status_code == 200:
            return r.text
        logger.debug("SOAP %s → HTTP %d\n%s", url, r.status_code, r.text[:300])
    except Exception as e:
        logger.debug("SOAP request error: %s", e)
    return None


def _xml_find(text: str, tag: str) -> str | None:
    m = re.search(rf"<[^>]*{re.escape(tag)}[^>]*>([^<]+)<", text)
    return m.group(1).strip() if m else None


def _xml_findall(text: str, tag: str) -> list[str]:
    return re.findall(rf"<[^>]*{re.escape(tag)}[^>]*token=\"([^\"]+)\"", text)


class CameraManager:
    def __init__(self, config: configparser.ConfigParser):
        self.config   = config
        self._running = False
        self._lock    = threading.Lock()

        if not config.has_section("camera"):
            self.enabled = False
            return

        cfg = config["camera"]
        self.enabled = cfg.getboolean("enabled", fallback=True)
        if not self.enabled:
            return

        self.name          = cfg.get("name", "Terrarium Cam")
        self.ip            = cfg.get("ip_address")
        self.port          = cfg.getint("onvif_port", fallback=2020)
        self.username      = cfg.get("username", "admin")
        self.password      = cfg.get("password", "")
        self.profile       = cfg.get("stream_profile", "sub").lower()
        self.snap_interval = cfg.getint("snapshot_interval_seconds", fallback=30)
        self.snap_path     = Path(cfg.get("snapshot_path", "data/snapshot.jpg"))

        # Timelapse
        self.timelapse         = cfg.getboolean("timelapse_enabled",      fallback=False)
        self.tl_path           = Path(cfg.get("timelapse_path",           "data/timelapse/"))
        self.tl_interval       = cfg.getint("timelapse_interval_seconds", fallback=300)
        self.tl_retention_days = cfg.getint("timelapse_retention_days",   fallback=7)
        self._last_tl_save     = 0.0

        self.snap_path.parent.mkdir(parents=True, exist_ok=True)
        if self.timelapse:
            self.tl_path.mkdir(parents=True, exist_ok=True)

        self._media_url     = f"http://{self.ip}:{self.port}/onvif/media_service"
        self._device_url    = f"http://{self.ip}:{self.port}/onvif/device_service"
        self._rtsp_uri      = None
        self._rtsp_uri_auth = None
        self._snapshot_uri  = None
        self._last_snap_ts  = None
        self._snap_ok       = False

        self._init_onvif()

    # ------------------------------------------------------------------
    # ONVIF discovery
    # ------------------------------------------------------------------

    def _init_onvif(self):
        try:
            logger.info("Connecting to camera at %s:%d ...", self.ip, self.port)

            resp = _soap(self._media_url, _GET_PROFILES, self.username, self.password)
            if not resp:
                resp = _soap(self._device_url, _GET_PROFILES, self.username, self.password)
            if not resp:
                logger.error("Could not retrieve ONVIF profiles from %s", self.ip)
                self.enabled = False
                return

            tokens = _xml_findall(resp, "Profiles")
            if not tokens:
                tokens = re.findall(r'token="([^"]+)"', resp)
            if not tokens:
                logger.error("No ONVIF profile tokens found in response.")
                self.enabled = False
                return

            logger.info("Found %d profile(s): %s", len(tokens), tokens)
            token = tokens[-1] if self.profile == "sub" and len(tokens) > 1 else tokens[0]
            logger.info("Using profile token: %s", token)

            resp = _soap(self._media_url, _GET_STREAM_URI.format(token=token),
                         self.username, self.password)
            if resp:
                uri = _xml_find(resp, "Uri")
                if uri:
                    self._rtsp_uri      = uri
                    self._rtsp_uri_auth = uri.replace(
                        "rtsp://", f"rtsp://{self.username}:{self.password}@"
                    )
                    logger.info("RTSP URI: %s", self._rtsp_uri)

            resp = _soap(self._media_url, _GET_SNAP_URI.format(token=token),
                         self.username, self.password)
            if resp:
                uri = _xml_find(resp, "Uri")
                if uri:
                    self._snapshot_uri = uri
                    logger.info("Snapshot URI: %s", self._snapshot_uri)

            if not self._rtsp_uri and not self._snapshot_uri:
                logger.error("Could not discover any stream URIs.")
                self.enabled = False
                return

            logger.info("Camera '%s' ready.", self.name)

        except Exception as e:
            logger.error("Camera init failed: %s", e, exc_info=True)
            self.enabled = False

    # ------------------------------------------------------------------
    # Snapshot capture
    # ------------------------------------------------------------------

    def _grab_snapshot_http(self) -> bool:
        if not self._snapshot_uri:
            logger.debug("No ONVIF snapshot URI — HTTP method unavailable.")
            return False
        last_status = None
        for label, auth in [("digest", requests.auth.HTTPDigestAuth(self.username, self.password)),
                            ("basic",  (self.username, self.password))]:
            try:
                r = requests.get(self._snapshot_uri, auth=auth, timeout=5, stream=True)
                last_status = r.status_code
                if r.status_code == 200:
                    with open(self.snap_path, "wb") as f:
                        for chunk in r.iter_content(8192):
                            f.write(chunk)
                    logger.debug("Snapshot saved via HTTP (%s auth).", label)
                    return True
            except Exception as e:
                logger.warning("HTTP snapshot request error (%s auth): %s", label, e)
        if last_status is not None:
            logger.warning("HTTP snapshot got HTTP %d from %s — likely auth or URI wrong.",
                           last_status, self._snapshot_uri)
        return False

    def _grab_snapshot_rtsp(self) -> bool:
        if not _CV2_OK or not self._rtsp_uri_auth:
            return False
        try:
            cap = cv2.VideoCapture(self._rtsp_uri_auth, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            ok, frame = cap.read()
            cap.release()
            if ok:
                cv2.imwrite(str(self.snap_path), frame)
                logger.debug("Snapshot saved via RTSP frame grab.")
                return True
        except Exception as e:
            logger.warning("RTSP snapshot failed: %s", e)
        return False

    def capture_snapshot(self) -> bool:
        ok = self._grab_snapshot_http() or self._grab_snapshot_rtsp()
        with self._lock:
            self._snap_ok = ok
            if ok:
                self._last_snap_ts = datetime.now()
        if ok:
            if self.timelapse:
                now = time.time()
                if now - self._last_tl_save >= self.tl_interval:
                    self._save_timelapse_frame()
                    self._last_tl_save = now
                    self._purge_timelapse()
        else:
            logger.warning("All snapshot methods failed for %s", self.name)
        return ok

    def _save_timelapse_frame(self):
        try:
            if not self.snap_path.exists():
                logger.warning("Timelapse: snapshot file missing, skipping.")
                return
            self.tl_path.mkdir(parents=True, exist_ok=True)
            dst = self.tl_path / f"frame_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
            shutil.copy2(self.snap_path, dst)
            logger.debug("Timelapse frame saved: %s", dst.name)
        except Exception as e:
            logger.warning("Timelapse save failed: %s", e)

    def _purge_timelapse(self):
        try:
            if not self.tl_path.exists():
                return
            cutoff  = time.time() - (self.tl_retention_days * 86400)
            removed = 0
            for f in self.tl_path.glob("frame_*.jpg"):
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            if removed:
                logger.info("Timelapse purge: removed %d frame(s) older than %d day(s).",
                            removed, self.tl_retention_days)
        except Exception as e:
            logger.warning("Timelapse purge failed: %s", e)

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def start(self):
        if not self.enabled:
            logger.info("Camera disabled – snapshot loop not started.")
            return
        self._running = True
        threading.Thread(target=self._loop, daemon=True, name="camera").start()
        logger.info("Camera snapshot loop started (every %ds).", self.snap_interval)

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            self.capture_snapshot()
            time.sleep(self.snap_interval)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            return {
                "name":          self.name,
                "enabled":       self.enabled,
                "ip":            self.ip,
                "rtsp_uri":      self._rtsp_uri or "",
                "rtsp_uri_auth": self._rtsp_uri_auth or "",
                "snapshot_ok":   self._snap_ok,
                "last_snapshot": self._last_snap_ts.strftime("%H:%M:%S")
                                 if self._last_snap_ts else "never",
                "timelapse":     self.timelapse,
            }

    @property
    def snapshot_path(self) -> Path:
        return self.snap_path

    @property
    def rtsp_uri(self) -> str | None:
        return self._rtsp_uri_auth