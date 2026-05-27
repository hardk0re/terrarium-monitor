"""
gecko_mood.py
Shared mood-scoring logic used by both the web dashboard and the OLED display
so the two surfaces always agree on whether the gecko is happy/neutral/upset.

Public API:
    compute_mood(config, data_logger) -> dict
        {
          "enabled":  bool,
          "mood":     "happy" | "neutral" | "upset" | "unknown",
          "summary":  short human-readable string,
          "reasons":  [{"score": 0|1|2, "label": str}, ...],
        }

Scoring: worst factor wins. 2 = happy, 1 = neutral, 0 = upset.
"""

from datetime import datetime


def compute_mood(config, data_logger) -> dict:
    if not config or not config.getboolean("gecko", "enabled", fallback=True):
        return {"enabled": False, "mood": "unknown", "summary": "", "reasons": []}

    g = lambda k, fb: config.getfloat("gecko", k, fallback=fb)
    t_min_h, t_max_h = g("temp_min_happy",     75.0), g("temp_max_happy",     88.0)
    t_min_c, t_max_c = g("temp_min_critical",  65.0), g("temp_max_critical",  95.0)
    h_min_h, h_max_h = g("humidity_min_happy", 40.0), g("humidity_max_happy", 70.0)
    h_min_c, h_max_c = g("humidity_min_critical", 25.0), g("humidity_max_critical", 90.0)
    warn_hours = g("checkin_warn_hours",     36.0)
    crit_hours = g("checkin_critical_hours", 72.0)
    sensors_filter = {s.strip() for s in
                      config.get("gecko", "sensors", fallback="").split(",") if s.strip()}

    temp_unit  = config.get("display", "temp_unit", fallback="F").upper()
    unit_label = "°F" if temp_unit == "F" else "°C"

    def band(val, min_h, max_h, min_c, max_c):
        if val < min_c or val > max_c: return 0
        if val < min_h or val > max_h: return 1
        return 2

    factors = []
    latest = data_logger.get_latest_readings() if data_logger else []
    for row in latest:
        if sensors_filter and row["sensor_name"] not in sensors_filter:
            continue
        tc = row["temp_c"]
        td = tc * 9/5 + 32 if temp_unit == "F" else tc
        factors.append((band(td, t_min_h, t_max_h, t_min_c, t_max_c),
                        f"{row['sensor_name']} temp {td:.1f}{unit_label}"))
        hv = row["humidity"]
        factors.append((band(hv, h_min_h, h_max_h, h_min_c, h_max_c),
                        f"{row['sensor_name']} humidity {hv:.1f}%"))

    # Most-recent check-in (care or feeding)
    last = None
    if data_logger:
        for cat in ("care", "feeding"):
            e = data_logger.get_last_event_by_category(cat)
            if e and (last is None or e["ts"] > last["ts"]):
                last = e
    if last is None:
        factors.append((0, "no check-in logged yet"))
    else:
        hours = (datetime.utcnow() - datetime.fromisoformat(last["ts"])).total_seconds() / 3600
        if   hours >= crit_hours: cs = 0
        elif hours >= warn_hours: cs = 1
        else:                     cs = 2
        if   hours < 1:  ago = f"{int(hours * 60)}m"
        elif hours < 48: ago = f"{int(hours)}h"
        else:            ago = f"{int(hours // 24)}d"
        factors.append((cs, f"last check-in {ago} ago"))

    if not factors:
        return {"enabled": True, "mood": "unknown",
                "summary": "Waiting for sensor data...", "reasons": []}

    worst = min(s for s, _ in factors)
    mood = "happy" if worst == 2 else "neutral" if worst == 1 else "upset"
    if mood == "happy":
        summary = "Cozy and content"
    elif mood == "neutral":
        summary = "A bit off — " + ", ".join(l for s, l in factors if s == 1)[:80]
    else:
        summary = "Needs attention: " + ", ".join(l for s, l in factors if s == 0)[:80]

    return {
        "enabled": True, "mood": mood, "summary": summary,
        "reasons": [{"score": s, "label": l} for s, l in factors],
    }
