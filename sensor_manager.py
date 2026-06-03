"""
sensor_manager.py
Discovers and reads all configured SHT31 sensors from config.ini.
"""

import time
import logging
import configparser
import board
import busio
import adafruit_sht31d

logger = logging.getLogger(__name__)


class SensorReading:
    def __init__(self, name: str, address: int, temperature_c: float, humidity: float):
        self.name = name
        self.address = address
        self.temperature_c = temperature_c
        self.humidity = humidity

    @property
    def temperature_f(self):
        return self.temperature_c * 9 / 5 + 32

    def __repr__(self):
        return (f"<SensorReading name={self.name!r} "
                f"temp={self.temperature_c:.1f}°C humidity={self.humidity:.1f}%>")


class SensorManager:
    def __init__(self, config: configparser.ConfigParser):
        self.config = config
        # Each entry: (name, address, sht31_device, temp_offset_c, humidity_offset)
        self.sensors = []
        self._init_sensors()

    def _init_sensors(self):
        """Discover all [sensor_N] sections and initialise I2C devices."""
        # Build i2c bus cache so we reuse the same bus object per bus number
        bus_cache: dict[int, busio.I2C] = {}

        for section in self.config.sections():
            if not section.lower().startswith("sensor_"):
                continue
            cfg = self.config[section]
            if not cfg.getboolean("enabled", fallback=True):
                logger.info("Sensor [%s] disabled, skipping.", section)
                continue

            name        = cfg.get("name", section)
            addr        = int(cfg.get("i2c_address", "0x44"), 16)
            bus_num     = cfg.getint("i2c_bus", 1)
            temp_offset = cfg.getfloat("temp_offset_c",  fallback=0.0)
            hum_offset  = cfg.getfloat("humidity_offset", fallback=0.0)

            if bus_num not in bus_cache:
                try:
                    if bus_num == 1:
                        bus_cache[bus_num] = busio.I2C(board.SCL, board.SDA)
                    else:
                        # secondary buses via /dev/i2c-N
                        import smbus2
                        bus_cache[bus_num] = smbus2.SMBus(bus_num)
                except Exception as e:
                    logger.error("Failed to open I2C bus %d: %s", bus_num, e)
                    continue

            try:
                device = adafruit_sht31d.SHT31D(bus_cache[bus_num], address=addr)
                self.sensors.append((name, addr, device, temp_offset, hum_offset))
                msg = f"Initialised sensor '{name}' at 0x{addr:02X} on bus {bus_num}"
                if temp_offset or hum_offset:
                    msg += f" (offsets: temp {temp_offset:+.2f}°C, hum {hum_offset:+.2f}%)"
                logger.info(msg)
            except Exception as e:
                logger.error("Failed to init sensor '%s' at 0x%02X: %s", name, addr, e)

    def read_all(self) -> list[SensorReading]:
        """Read temperature and humidity from every configured sensor,
        applying per-sensor calibration offsets."""
        readings = []
        for name, addr, device, temp_offset, hum_offset in self.sensors:
            try:
                temp = device.temperature + temp_offset       # °C, calibrated
                hum  = device.relative_humidity + hum_offset  # %,  calibrated
                # Humidity is physically bounded; clamp to keep it sane.
                hum = max(0.0, min(100.0, hum))
                readings.append(SensorReading(name, addr, temp, hum))
                logger.debug("Sensor '%s': %.2f°C  %.2f%%RH", name, temp, hum)
            except Exception as e:
                logger.warning("Read failed for sensor '%s': %s", name, e)
        return readings

    def average_temperature(self, readings: list[SensorReading]) -> float | None:
        temps = [r.temperature_c for r in readings]
        return sum(temps) / len(temps) if temps else None

    def average_humidity(self, readings: list[SensorReading]) -> float | None:
        hums = [r.humidity for r in readings]
        return sum(hums) / len(hums) if hums else None

    def temperature_for(self, readings: list[SensorReading],
                        sensor_name: str = "") -> float | None:
        """Return the temperature (°C) from the named sensor, or the average
        of all sensors if sensor_name is blank. Returns None if the named
        sensor isn't in the current readings (e.g. it failed this poll)."""
        if sensor_name:
            for r in readings:
                if r.name.lower() == sensor_name.lower():
                    return r.temperature_c
            return None
        return self.average_temperature(readings)

    def humidity_for(self, readings: list[SensorReading],
                     sensor_name: str = "") -> float | None:
        """Same as temperature_for but for humidity (%)."""
        if sensor_name:
            for r in readings:
                if r.name.lower() == sensor_name.lower():
                    return r.humidity
            return None
        return self.average_humidity(readings)
