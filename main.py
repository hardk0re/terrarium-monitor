"""
main.py  –  Terrarium Monitor entry point
Run with:  python main.py
Or via systemd service (see terrarium.service)
"""

import time
import signal
import logging
import configparser
import threading
from pathlib import Path

# ── project modules ───────────────────────────────────────────────────
from sensor_manager   import SensorManager
from data_logger      import DataLogger
from relay_controller import build_mister, build_fan, cleanup_gpio
from tapo_controller  import LightingController
from display_manager  import DisplayManager
from camera_manager   import CameraManager
from weather_manager  import WeatherManager
from pushover_notifier import PushoverNotifier
from care_button       import CareButton
from gecko_mood        import compute_mood
import web_dashboard

# ── logging setup ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / "data" / "terrarium.log"),
    ]
)
logger = logging.getLogger("main")

# ── config ────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "config.ini"


def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    return cfg


# ── subsystem helpers ─────────────────────────────────────────────────

def build_subsystems(cfg: configparser.ConfigParser) -> dict:
    """Instantiate all controllable subsystems from config."""
    dl       = DataLogger(cfg)
    sensors  = SensorManager(cfg)
    mister   = build_mister(cfg, dl)
    fan      = build_fan(cfg, dl)
    lighting = LightingController(cfg, data_logger=dl)
    camera   = CameraManager(cfg)
    weather  = WeatherManager(cfg, data_logger=dl)
    return dict(dl=dl, sensors=sensors, mister=mister,
                fan=fan, lighting=lighting, camera=camera, weather=weather)


def reload_subsystems(current: dict, cfg: configparser.ConfigParser) -> dict:
    """Gracefully stop current subsystems and rebuild from updated config."""
    logger.info("Reloading subsystems from updated config...")

    # Preserve last weather reading so display doesn't go blank
    last_weather = None
    if current.get("weather") and current["weather"].enabled:
        last_weather = current["weather"].latest()

    try:
        current["lighting"].stop()
        current["camera"].stop()
        current["weather"].stop()
        if current["mister"]: current["mister"].force_off()
        if current["fan"]:    current["fan"].force_off()
    except Exception as e:
        logger.warning("Error stopping subsystems during reload: %s", e)

    new = build_subsystems(cfg)
    new["lighting"].start_schedule_loop()
    new["camera"].start()
    new["weather"].start()

    # Restore last reading so display keeps showing weather immediately
    if last_weather and new["weather"].enabled:
        with new["weather"]._lock:
            new["weather"]._latest = last_weather

    return new


# ── entry point ───────────────────────────────────────────────────────

def main():
    cfg  = load_config()
    subs = build_subsystems(cfg)
    dl, sensors, mister, fan, lighting, camera, weather = (
        subs["dl"], subs["sensors"], subs["mister"],
        subs["fan"], subs["lighting"], subs["camera"], subs["weather"]
    )
    display = DisplayManager(cfg, sensors, mister, fan, lighting, weather,
                             data_logger=dl)

    # ── Pushover notifications ────────────────────────────────────────
    notifier = PushoverNotifier(cfg)
    dl.set_notifier(notifier)
    last_mood = None  # tracks gecko mood transitions across polls

    # ── Physical buttons (up to 3) ────────────────────────────────────
    care_buttons = []
    seen_pins    = set()
    for sec in ("care_button_1", "care_button_2", "care_button_3"):
        if not cfg.has_section(sec):
            continue
        b = CareButton(cfg, dl, sec)
        if b.pin is not None and b.pin not in seen_pins:
            care_buttons.append(b)
            seen_pins.add(b.pin)
        elif b.pin is not None:
            logger.warning("%s: GPIO %d already in use, skipping.", sec, b.pin)

    # ── Sensor failure tracking ───────────────────────────────────────
    # Per-sensor last successful read time; sensors currently in "failed"
    # state so we only notify on transitions (not every poll).
    from datetime import datetime as _dt
    sensor_last_good = {n: _dt.utcnow() for n, _, _ in sensors.sensors}
    sensor_failed = set()

    # ── Reload / shutdown events ──────────────────────────────────────
    _stop   = threading.Event()
    _reload = threading.Event()

    def _do_reload():
        _reload.set()

    def _shutdown(sig, frame):
        logger.info("Shutdown signal received.")
        _stop.set()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # ── Wire web dashboard ────────────────────────────────────────────
    web_dashboard.init(cfg, dl, sensors, mister, fan, lighting, camera, weather,
                       notifier=notifier)
    web_dashboard.set_reload_callback(_do_reload)

    # ── Start background services ─────────────────────────────────────
    lighting.start_schedule_loop()
    camera.start()
    weather.start()
    display.start()

    if cfg.getboolean("web", "enabled", fallback=True):
        threading.Thread(
            target=web_dashboard.run, args=(cfg,),
            daemon=True, name="web"
        ).start()

    poll_sec = cfg.getint("general", "poll_interval_seconds", fallback=30)
    logger.info("Terrarium monitor running. Poll every %ds.", poll_sec)
    dl.log_system_event("startup", f"Terrarium monitor started. Poll interval: {poll_sec}s")

    # ── Main control loop ─────────────────────────────────────────────
    try:
        while not _stop.is_set():

            # Config reload requested by web UI
            if _reload.is_set():
                _reload.clear()
                cfg.read(CONFIG_PATH)
                subs = reload_subsystems(subs, cfg)
                dl, sensors, mister, fan, lighting, camera, weather = (
                    subs["dl"], subs["sensors"], subs["mister"],
                    subs["fan"], subs["lighting"], subs["camera"], subs["weather"]
                )
                display.update_components(sensors, mister, fan, lighting, weather,
                                          data_logger=dl)
                notifier.reload(cfg)
                dl.set_notifier(notifier)
                web_dashboard.init(cfg, dl, sensors, mister, fan, lighting, camera, weather,
                                   notifier=notifier)
                web_dashboard.set_reload_callback(_do_reload)
                poll_sec = cfg.getint("general", "poll_interval_seconds", fallback=30)
                logger.info("Subsystems reloaded successfully.")
                dl.log_system_event("config", "Configuration reloaded via web UI")

            # Sensor read + control logic
            readings = sensors.read_all()

            # Sensor failure detection — compare configured-and-initialised
            # sensors vs. who actually reported this cycle. Notify on transitions.
            try:
                threshold_min = cfg.getfloat("general", "sensor_failure_alert_minutes",
                                             fallback=10.0)
                if threshold_min > 0:
                    now_utc = _dt.utcnow()
                    enabled_names = {n for n, _, _ in sensors.sensors}
                    got_names     = {r.name for r in readings}
                    # Track any newly-added sensors (e.g. after a reload)
                    for n in enabled_names:
                        sensor_last_good.setdefault(n, now_utc)
                    # Drop tracking for sensors no longer enabled
                    for n in list(sensor_last_good):
                        if n not in enabled_names:
                            sensor_last_good.pop(n, None)
                            sensor_failed.discard(n)
                    # Compare each enabled sensor
                    for n in enabled_names:
                        if n in got_names:
                            sensor_last_good[n] = now_utc
                            if n in sensor_failed:
                                sensor_failed.discard(n)
                                dl.log_system_event("error",
                                    f"Sensor '{n}' recovered.")
                        else:
                            minutes_silent = (now_utc - sensor_last_good[n]).total_seconds() / 60
                            if n not in sensor_failed and minutes_silent >= threshold_min:
                                sensor_failed.add(n)
                                dl.log_system_event("error",
                                    f"Sensor '{n}' unresponsive for "
                                    f"{int(minutes_silent)} min.")
            except Exception as e:
                logger.warning("Sensor failure check failed: %s", e)

            if readings:
                dl.log_readings(readings)
                dl.purge_old_data()
                display.update_readings(readings)

            # Gecko mood transitions → Pushover. Runs even when sensors fail,
            # because mood is also driven by check-in age (care/feeding logs).
            # notify_mood_change handles its own enabled/configured checks.
            try:
                mood_info = compute_mood(cfg, dl)
                new_mood  = mood_info.get("mood")
                summary   = mood_info.get("summary", "")
                if new_mood and new_mood != "unknown":
                    fire = False
                    if last_mood is None:
                        # First observed mood after startup: only alert if it's
                        # already a problem state worth flagging.
                        fire = (new_mood != "happy")
                        prev_label = "(startup)"
                    elif new_mood != last_mood:
                        fire = True
                        prev_label = last_mood
                    if fire:
                        notifier.notify_mood_change(prev_label, new_mood, summary)
                        msg = f"Mood: {prev_label} → {new_mood}"
                        if summary:
                            msg += f" ({summary})"
                        # Log to the event log table so it shows in the /logs viewer.
                        # Note: this also runs through notify_event for category
                        # "gecko" — but notify_gecko isn't a configured flag so
                        # it stays silent and we don't double-notify.
                        dl.log_system_event("gecko", msg)
                        logger.info("Gecko mood: %s → %s (%s)",
                                    prev_label, new_mood, summary)
                    last_mood = new_mood
            except Exception as e:
                logger.warning("Mood check failed: %s", e)

            avg_hum  = sensors.average_humidity(readings)
            avg_temp = sensors.average_temperature(readings)

            if mister and avg_hum is not None:
                threshold = cfg.getfloat("mister", "humidity_threshold_low", fallback=50.0)
                if avg_hum < threshold and not mister.is_on and not mister.in_cooldown:
                    logger.info("Humidity %.1f%% < %.1f%% – triggering mister.",
                                avg_hum, threshold)
                    mister.trigger(reason=f"humidity {avg_hum:.1f}% < {threshold}%")

            if fan and avg_temp is not None:
                # Unit the fan thresholds are written in. [fan].temp_unit
                # overrides, else fall back to [display].temp_unit. avg_temp
                # is always Celsius from the sensor; convert to comparison unit.
                fan_unit = cfg.get("fan", "temp_unit", fallback="").strip().upper()
                if fan_unit not in ("F", "C"):
                    fan_unit = cfg.get("display", "temp_unit", fallback="F").upper()
                avg_t = avg_temp * 9 / 5 + 32 if fan_unit == "F" else avg_temp
                unit_lbl = "°F" if fan_unit == "F" else "°C"
                # Defaults track the unit: ~85/65 °F vs 30/18 °C.
                t_high = cfg.getfloat("fan", "temp_threshold_high",
                                      fallback=85.0 if fan_unit == "F" else 30.0)
                t_low  = cfg.getfloat("fan", "temp_threshold_low",
                                      fallback=65.0 if fan_unit == "F" else 18.0)
                if avg_t > t_high and not fan.is_on and not fan.in_cooldown:
                    logger.info("Temp %.1f%s > %.1f%s – triggering fan (too hot).",
                                avg_t, unit_lbl, t_high, unit_lbl)
                    fan.trigger(reason=f"temp {avg_t:.1f}{unit_lbl} > {t_high}{unit_lbl} (too hot)")
                elif avg_t < t_low and not fan.is_on and not fan.in_cooldown:
                    logger.info("Temp %.1f%s < %.1f%s – triggering fan (too cold).",
                                avg_t, unit_lbl, t_low, unit_lbl)
                    fan.trigger(reason=f"temp {avg_t:.1f}{unit_lbl} < {t_low}{unit_lbl} (too cold)")

            # Sleep in small chunks so stop/reload events respond quickly
            for _ in range(poll_sec * 2):
                if _stop.is_set() or _reload.is_set():
                    break
                time.sleep(0.5)

    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Cleaning up...")
        dl.log_system_event("shutdown", "Terrarium monitor shutting down")
        display.stop()
        camera.stop()
        weather.stop()
        # Stop the schedule loop but leave the Tapo plugs in whatever state
        # they were in — we shouldn't yank UVB / basking lights when restarting.
        lighting.stop()
        for b in care_buttons:
            b.stop()
        if mister: mister.force_off()
        if fan:    fan.force_off()
        cleanup_gpio()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()