"""
data_logger.py
SQLite-backed logger for sensor readings, relay events, weather and system events.
Automatically purges records older than log_retention_days.
"""

import sqlite3
import logging
import configparser
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "data" / "terrarium.db"


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    """Create tables if they don't exist."""
    with _conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS sensor_readings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT    NOT NULL,
                sensor_name TEXT    NOT NULL,
                temp_c      REAL    NOT NULL,
                humidity    REAL    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS relay_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT    NOT NULL,
                relay_name  TEXT    NOT NULL,
                action      TEXT    NOT NULL,
                reason      TEXT
            );

            CREATE TABLE IF NOT EXISTS weather_readings (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           TEXT    NOT NULL,
                temp_c       REAL    NOT NULL,
                feels_like_c REAL,
                humidity     INTEGER,
                description  TEXT,
                wind_speed   REAL,
                city         TEXT
            );

            CREATE TABLE IF NOT EXISTS system_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT    NOT NULL,
                category    TEXT    NOT NULL,
                message     TEXT    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_readings_ts  ON sensor_readings(ts);
            CREATE INDEX IF NOT EXISTS idx_weather_ts   ON weather_readings(ts);
            CREATE INDEX IF NOT EXISTS idx_events_ts    ON relay_events(ts);
            CREATE INDEX IF NOT EXISTS idx_sysevents_ts ON system_events(ts);
        """)
    logger.info("Database initialised at %s", DB_PATH)


class DataLogger:
    def __init__(self, config: configparser.ConfigParser):
        self.retention_days = config.getint("general", "log_retention_days", fallback=31)
        init_db()

    # ------------------------------------------------------------------
    # Write helpers
    # ------------------------------------------------------------------

    def log_readings(self, readings):
        """Persist a list of SensorReading objects."""
        ts   = datetime.utcnow().isoformat()
        rows = [(ts, r.name, r.temperature_c, r.humidity) for r in readings]
        with _conn() as con:
            con.executemany(
                "INSERT INTO sensor_readings (ts, sensor_name, temp_c, humidity) VALUES (?,?,?,?)",
                rows
            )
        logger.debug("Logged %d sensor reading(s).", len(rows))

    def log_relay_event(self, relay_name: str, action: str, reason: str = ""):
        ts = datetime.utcnow().isoformat()
        with _conn() as con:
            con.execute(
                "INSERT INTO relay_events (ts, relay_name, action, reason) VALUES (?,?,?,?)",
                (ts, relay_name, action, reason)
            )
        self.log_system_event(
            "relay",
            f"{relay_name} → {action}" + (f" ({reason})" if reason else "")
        )
        logger.info("Relay event: %s → %s (%s)", relay_name, action, reason)

    def log_system_event(self, category: str, message: str):
        """Log a general system event — startup, config change, lights, etc."""
        ts = datetime.utcnow().isoformat()
        with _conn() as con:
            con.execute(
                "INSERT INTO system_events (ts, category, message) VALUES (?,?,?)",
                (ts, category, message)
            )
        logger.debug("System event [%s]: %s", category, message)

    def log_weather(self, reading: dict):
        """Persist an outdoor weather reading."""
        with _conn() as con:
            con.execute(
                """INSERT INTO weather_readings
                   (ts, temp_c, feels_like_c, humidity, description, wind_speed, city)
                   VALUES (?,?,?,?,?,?,?)""",
                (reading["ts"], reading["temp_c"], reading.get("feels_like_c"),
                 reading.get("humidity"), reading.get("description"),
                 reading.get("wind_speed"), reading.get("city"))
            )
        logger.debug("Weather reading logged.")

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def get_readings(self, hours: int = 24, sensor_name: str = None) -> list[dict]:
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        with _conn() as con:
            if sensor_name:
                rows = con.execute(
                    "SELECT * FROM sensor_readings WHERE ts >= ? AND sensor_name = ? ORDER BY ts",
                    (since, sensor_name)
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM sensor_readings WHERE ts >= ? ORDER BY ts",
                    (since,)
                ).fetchall()
        return [dict(r) for r in rows]

    def get_latest_readings(self) -> list[dict]:
        """Most recent reading per sensor."""
        with _conn() as con:
            rows = con.execute("""
                SELECT s.* FROM sensor_readings s
                INNER JOIN (
                    SELECT sensor_name, MAX(ts) AS max_ts
                    FROM sensor_readings GROUP BY sensor_name
                ) latest ON s.sensor_name = latest.sensor_name AND s.ts = latest.max_ts
                ORDER BY s.sensor_name
            """).fetchall()
        return [dict(r) for r in rows]

    def get_relay_run_count(self, relay_name: str, hours: int = 24) -> int:
        """Count ON events for a relay in the last N hours."""
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        with _conn() as con:
            row = con.execute(
                "SELECT COUNT(*) FROM relay_events WHERE relay_name=? AND action='ON' AND ts>=?",
                (relay_name, since)
            ).fetchone()
        return row[0] if row else 0

    def get_relay_events(self, hours: int = 24) -> list[dict]:
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        with _conn() as con:
            rows = con.execute(
                "SELECT * FROM relay_events WHERE ts >= ? ORDER BY ts DESC",
                (since,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_system_events(self, hours: int = 24) -> list[dict]:
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        with _conn() as con:
            rows = con.execute(
                "SELECT * FROM system_events WHERE ts >= ? ORDER BY ts DESC",
                (since,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_last_event_by_category(self, category: str) -> dict | None:
        """Return the most recent system_events row for the given category, or None."""
        with _conn() as con:
            row = con.execute(
                "SELECT ts, category, message FROM system_events "
                "WHERE category = ? ORDER BY ts DESC LIMIT 1",
                (category,)
            ).fetchone()
        return dict(row) if row else None

    def get_all_events(self, hours: int = 24) -> list[dict]:
        """Merge relay events and system events into one sorted list."""
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        with _conn() as con:
            relay_rows = con.execute(
                """SELECT ts, 'relay' as category,
                   relay_name || ' → ' || action ||
                   CASE WHEN reason != '' THEN ' (' || reason || ')' ELSE '' END as message
                   FROM relay_events WHERE ts >= ?""",
                (since,)
            ).fetchall()
            sys_rows = con.execute(
                "SELECT ts, category, message FROM system_events WHERE ts >= ?",
                (since,)
            ).fetchall()
        combined = [dict(r) for r in relay_rows] + [dict(r) for r in sys_rows]
        combined.sort(key=lambda r: r["ts"], reverse=True)
        return combined

    def get_weather_readings(self, hours: int = 24) -> list[dict]:
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        with _conn() as con:
            rows = con.execute(
                "SELECT * FROM weather_readings WHERE ts >= ? ORDER BY ts",
                (since,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_latest_weather(self) -> dict | None:
        with _conn() as con:
            row = con.execute(
                "SELECT * FROM weather_readings ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def purge_old_data(self):
        cutoff = (datetime.utcnow() - timedelta(days=self.retention_days)).isoformat()
        with _conn() as con:
            n1 = con.execute("DELETE FROM sensor_readings  WHERE ts < ?", (cutoff,)).rowcount
            n2 = con.execute("DELETE FROM relay_events     WHERE ts < ?", (cutoff,)).rowcount
            n3 = con.execute("DELETE FROM system_events    WHERE ts < ?", (cutoff,)).rowcount
            n4 = con.execute("DELETE FROM weather_readings WHERE ts < ?", (cutoff,)).rowcount
        if n1 or n2 or n3 or n4:
            logger.info("Purged %d reading(s), %d relay event(s), %d system event(s), "
                        "%d weather reading(s) older than %d days.",
                        n1, n2, n3, n4, self.retention_days)