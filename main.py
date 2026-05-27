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
    web_dashboard.init(cfg, dl, sensors, mister, fan, lighting, camera, weather)
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
                web_dashboard.init(cfg, dl, sensors, mister, fan, lighting, camera, weather)
                web_dashboard.set_reload_callback(_do_reload)
                poll_sec = cfg.getint("general", "poll_interval_seconds", fallback=30)
                logger.info("Subsystems reloaded successfully.")
                dl.log_system_event("config", "Configuration reloaded via web UI")

            # Sensor read + control logic
            readings = sensors.read_all()
            if readings:
                dl.log_readings(readings)
                dl.purge_old_data()
                display.update_readings(readings)

            avg_hum  = sensors.average_humidity(readings)
            avg_temp = sensors.average_temperature(readings)

            if mister and avg_hum is not None:
                threshold = cfg.getfloat("mister", "humidity_threshold_low", fallback=50.0)
                if avg_hum < threshold and not mister.is_on and not mister.in_cooldown:
                    logger.info("Humidity %.1f%% < %.1f%% – triggering mister.",
                                avg_hum, threshold)
                    mister.trigger(reason=f"humidity {avg_hum:.1f}% < {threshold}%")

            if fan and avg_temp is not None:
                t_high = cfg.getfloat("fan", "temp_threshold_high", fallback=30.0)
                t_low  = cfg.getfloat("fan", "temp_threshold_low",  fallback=20.0)
                if avg_temp > t_high and not fan.is_on and not fan.in_cooldown:
                    logger.info("Temp %.1f°C > %.1f°C – triggering fan (too hot).", avg_temp, t_high)
                    fan.trigger(reason=f"temp {avg_temp:.1f}°C > {t_high}°C (too hot)")
                elif avg_temp < t_low and not fan.is_on and not fan.in_cooldown:
                    logger.info("Temp %.1f°C < %.1f°C – triggering fan (too cold).", avg_temp, t_low)
                    fan.trigger(reason=f"temp {avg_temp:.1f}°C < {t_low}°C (too cold)")

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
        lighting.force_all_off()
        if mister: mister.force_off()
        if fan:    fan.force_off()
        cleanup_gpio()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()