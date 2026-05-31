"""
web_dashboard.py
Flask web dashboard with live status, history charts, manual overrides,
and a full config editor page (auth-protected if enabled).
"""

import logging
import configparser
from datetime import datetime, timezone, timedelta
from pathlib import Path
from functools import wraps
from flask import Flask, jsonify, request, Response, redirect, url_for, session

logger = logging.getLogger(__name__)
app = Flask(__name__)
# generate a new Key: python3 -c "import secrets; print(secrets.token_hex(32))"
app.secret_key = "a48af364f1b148da855cb51057ecfa179bc7c572b439dee84bf0f49fa6fcfe60"

CONFIG_PATH = Path(__file__).parent / "config.ini"

def _utc_to_local(ts: str) -> str:
    """Convert UTC ISO timestamp to local time using utc_offset_hours from config."""
    try:
        offset_hours = _config.getfloat("general", "utc_offset_hours", fallback=0) if _config else 0
        dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        local = dt + timedelta(hours=offset_hours)
        return local.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ts.replace("T", " ")[:16]

# Injected by main.py
_data_logger     = None
_sensor_mgr      = None
_mister          = None
_fan             = None
_lighting        = None
_camera          = None
_weather         = None
_notifier        = None
_config          = None
_reload_callback = None


def set_reload_callback(fn):
    global _reload_callback
    _reload_callback = fn


def init(config, data_logger, sensor_manager, mister, fan, lighting,
         camera=None, weather=None, notifier=None):
    global _data_logger, _sensor_mgr, _mister, _fan, _lighting, _camera, _weather, _notifier, _config
    _data_logger  = data_logger
    _sensor_mgr   = sensor_manager
    _mister       = mister
    _fan          = fan
    _lighting     = lighting
    _camera       = camera
    _weather      = weather
    _notifier     = notifier
    _config       = config


# ------------------------------------------------------------------
# Auth helpers
# ------------------------------------------------------------------

def _auth_enabled():
    return _config and _config.getboolean("web", "auth_enabled", fallback=False)

def _config_protected():
    return _config and _config.getboolean("web", "config_protected", fallback=True)

def _check_credentials(username, password):
    return (username == _config.get("web", "username", fallback="admin") and
            password == _config.get("web", "password", fallback="terrarium"))

def _check_config_credentials(username, password):
    return (username == _config.get("web", "config_username", fallback="admin") and
            password == _config.get("web", "config_password", fallback="terrarium"))

def _auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _auth_enabled():
            return f(*args, **kwargs)
        if session.get("logged_in"):
            return f(*args, **kwargs)
        auth = request.authorization
        if auth and _check_credentials(auth.username, auth.password):
            return f(*args, **kwargs)
        if request.accept_mimetypes.accept_html:
            return redirect(url_for("login", next=request.path))
        return Response("Unauthorised", 401,
                        {"WWW-Authenticate": 'Basic realm="Terrarium"'})
    return decorated


def _config_auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # If full auth is on and user is already logged in, allow through
        if _auth_enabled() and session.get("logged_in"):
            return f(*args, **kwargs)
        # Check config-specific session flag
        if session.get("config_logged_in"):
            return f(*args, **kwargs)
        # If config_protected is off, allow through
        if not _config_protected():
            return f(*args, **kwargs)
        # Check HTTP Basic for API access
        auth = request.authorization
        if auth and _check_config_credentials(auth.username, auth.password):
            return f(*args, **kwargs)
        # Redirect browser to config login
        if request.accept_mimetypes.accept_html:
            return redirect(url_for("config_login", next=request.path))
        return Response("Unauthorised", 401,
                        {"WWW-Authenticate": 'Basic realm="Terrarium Config"'})
    return decorated


# ------------------------------------------------------------------
# Login / Logout
# ------------------------------------------------------------------

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Terrarium Login</title>
<style>
  body{background:#0a0a14;color:#e0e0f0;font-family:system-ui,sans-serif;
       display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
  .box{background:#14142a;padding:2rem;border-radius:16px;width:100%;max-width:360px}
  h2{color:#00c878;margin-bottom:1.5rem;text-align:center}
  input{width:100%;padding:.6rem .8rem;margin-bottom:1rem;background:#0a0a20;
        border:1px solid #22224a;border-radius:8px;color:#e0e0f0;font-size:1rem;box-sizing:border-box}
  button{width:100%;padding:.7rem;background:#00c878;color:#000;border:0;
         border-radius:8px;font-weight:700;font-size:1rem;cursor:pointer}
  .err{color:#ff5028;margin-bottom:1rem;text-align:center;font-size:.9rem}
</style>
</head>
<body>
<div class="box">
  <h2>🦎 Terrarium Monitor</h2>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <form method="POST">
    <input name="username" placeholder="Username" autofocus>
    <input name="password" type="password" placeholder="Password">
    <button type="submit">Sign In</button>
  </form>
</div>
</body>
</html>"""

@app.route("/config-login", methods=["GET", "POST"])
def config_login():
    from flask import render_template_string
    error = None
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if _check_config_credentials(u, p):
            session["config_logged_in"] = True
            return redirect(request.args.get("next") or "/config")
        error = "Invalid username or password."
    html = LOGIN_HTML.replace("🦎 Terrarium Monitor", "⚙️ Terrarium Config")
    return render_template_string(html, error=error)


@app.route("/config-logout")
def config_logout():
    session.pop("config_logged_in", None)
    return redirect(url_for("config_login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    from flask import render_template_string
    error = None
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if _check_credentials(u, p):
            session["logged_in"] = True
            return redirect(request.args.get("next") or "/")
        error = "Invalid username or password."
    return render_template_string(LOGIN_HTML, error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------------------------
# Config editor page
# ------------------------------------------------------------------

CONFIG_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Terrarium Config</title>
<style>
  :root{--bg:#0a0a14;--card:#14142a;--accent:#00c878;--text:#e0e0f0;--grey:#7878a0;--warn:#ff5028}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:system-ui,sans-serif;padding:1rem}
  h1{color:var(--accent);margin-bottom:.5rem}
  .nav{margin-bottom:1.5rem}
  .nav a{color:var(--accent);text-decoration:none;margin-right:1rem;font-size:.9rem}
  .section{background:var(--card);border-radius:12px;padding:1rem;margin-bottom:1rem}
  .section h3{color:var(--accent);font-size:.85rem;text-transform:uppercase;
              letter-spacing:.08em;margin-bottom:.8rem;border-bottom:1px solid #22224a;padding-bottom:.4rem}
  .row{display:grid;grid-template-columns:200px 1fr;gap:.5rem;align-items:center;margin-bottom:.5rem}
  label{font-size:.85rem;color:var(--grey)}
  input[type=text]{width:100%;padding:.4rem .6rem;background:#0a0a20;border:1px solid #22224a;
                   border-radius:6px;color:var(--text);font-size:.9rem}
  input[type=text]:focus{outline:none;border-color:var(--accent)}
  .actions{display:flex;gap:.5rem;margin-top:1rem;position:sticky;bottom:0;
           background:var(--bg);padding:.5rem 0}
  button{padding:.5rem 1.4rem;border:0;border-radius:8px;font-weight:600;cursor:pointer}
  .save-btn{background:var(--accent);color:#000}
  .back-btn{background:#333;color:#ccc}
  .msg{padding:.5rem 1rem;border-radius:8px;margin-bottom:1rem;font-size:.9rem}
  .msg.ok{background:#0a2a1a;color:var(--accent)}
  .msg.err{background:#2a0a0a;color:var(--warn)}
  .comment{font-size:.75rem;color:#555;font-style:italic;grid-column:2}
</style>
</head>
<body>
<h1>⚙️ Configuration Editor</h1>
<div class="nav">
  <a href="/">← Dashboard</a>
  <a href="/config-logout">Logout</a>
  <button id="pushover-test-btn" style="background:#3a87f0;color:#fff;padding:.3rem .9rem;border-radius:8px;border:0;font-weight:600;cursor:pointer;font-size:.85rem;margin-left:.5rem" onclick="testPushover()">&#128276; Test Pushover</button>
  <span id="pushover-test-msg" style="margin-left:.6rem;font-size:.85rem;color:#7878a0"></span>
  <button id="restart-btn" style="background:#ff8800;color:#000;padding:.3rem .9rem;border-radius:8px;border:0;font-weight:600;cursor:pointer;font-size:.85rem;margin-left:.5rem" onclick="restartPython()">&#8635; Restart</button>
</div>
<script>
async function testPushover(){
  const btn = document.getElementById("pushover-test-btn");
  const msg = document.getElementById("pushover-test-msg");
  btn.disabled = true; const oldText = btn.textContent;
  btn.textContent = "Sending...";
  msg.style.color = "#7878a0";
  msg.textContent = "";
  try {
    const r = await fetch("/api/pushover/test", {method: "POST"});
    const d = await r.json();
    msg.textContent = d.message || (d.ok ? "Sent." : "Failed.");
    msg.style.color = d.ok ? "#00c878" : "#ff5028";
  } catch(e){
    msg.textContent = "Request error: " + e;
    msg.style.color = "#ff5028";
  } finally {
    btn.disabled = false; btn.textContent = oldText;
    setTimeout(()=>{ msg.textContent = ""; }, 6000);
  }
}
async function restartPython(){
  if(!confirm("Restart the Python process? Controls will be offline briefly while systemd restarts the service.")) return;
  const btn=document.getElementById("restart-btn");
  btn.textContent="Restarting..."; btn.disabled=true;
  try{ await fetch("/api/shutdown",{method:"POST"}); }catch(e){}
  // systemd brings the service back after RestartSec; reload the page once it's likely up
  setTimeout(()=>{ window.location.reload(); }, 12000);
}
</script>

{% if message %}
<div class="msg {{ 'ok' if message_ok else 'err' }}">{{ message }}</div>
{% endif %}

<form method="POST" action="/config">
  {% for section, items in sections.items() %}
  <div class="section">
    <h3>[{{ section }}]</h3>
    {% for key, value, comment in items %}
    <div class="row">
      <label for="{{ section }}__{{ key }}">{{ key }}</label>
      <div>
        <input type="text" id="{{ section }}__{{ key }}"
               name="{{ section }}__{{ key }}" value="{{ value }}">
        {% if comment %}<div class="comment">{{ comment }}</div>{% endif %}
      </div>
    </div>
    {% endfor %}
  </div>
  {% endfor %}
  <div class="actions">
    <button type="submit" class="save-btn">💾 Save &amp; Restart Config</button>
    <a href="/"><button type="button" class="back-btn">Cancel</button></a>
  </div>
</form>
</body>
</html>"""

def _read_config_with_comments():
    """Read config.ini using configparser directly — reliable section/key extraction."""
    sections = {}
    try:
        cfg = configparser.ConfigParser()
        cfg.read(CONFIG_PATH)
        for section in cfg.sections():
            sections[section] = []
            for key, value in cfg.items(section):
                sections[section].append((key, value, ""))
    except Exception as e:
        logger.error("Failed to read config for editor: %s", e)
    return sections

@app.route("/config", methods=["GET", "POST"])
@_config_auth_required
def config_page():
    from flask import render_template_string
    message    = None
    message_ok = False

    if request.method == "POST":
        try:
            # Load existing config
            cfg = configparser.ConfigParser()
            cfg.read(CONFIG_PATH)

            # Apply submitted values
            for field, value in request.form.items():
                if "__" in field:
                    section, _, key = field.partition("__")
                    if cfg.has_section(section):
                        cfg.set(section, key, value.strip())

            # Write back
            with open(CONFIG_PATH, "w") as f:
                cfg.write(f)

            # Reload global config
            global _config
            _config.read(CONFIG_PATH)

            # Reload subsystems in main thread via event
            if _reload_callback:
                _reload_callback()

            message    = "✅ Config saved! Subsystems are reloading..."
            message_ok = True
            logger.info("Config updated via web UI.")
        except Exception as e:
            message    = f"❌ Save failed: {e}"
            message_ok = False

    sections     = _read_config_with_comments()
    auth_enabled = _auth_enabled()
    return render_template_string(CONFIG_HTML,
                                  sections=sections,
                                  message=message,
                                  message_ok=message_ok,
                                  auth_enabled=auth_enabled)


# ------------------------------------------------------------------
# Main dashboard HTML
# ------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ site_title }}</title>
<style>
  :root{--bg:#0a0a14;--card:#14142a;--accent:#00c878;--warn:#ff5028;--cool:#50a0ff;--text:#e0e0f0;--grey:#7878a0}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:system-ui,sans-serif;padding:1rem}
  .topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem}
  h1{color:var(--accent);font-size:1.4rem}
  .topbar-btns{display:flex;gap:.5rem}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:1rem;margin-bottom:1.5rem}
  .card{background:var(--card);border-radius:12px;padding:1rem}
  .card h3{font-size:.8rem;color:var(--grey);text-transform:uppercase;letter-spacing:.05em;margin-bottom:.4rem}
  .big{font-size:2.4rem;font-weight:700;line-height:1}
  .sub{font-size:.9rem;color:var(--grey);margin-top:.25rem}
  .badge{display:inline-block;padding:.2rem .6rem;border-radius:99px;font-size:.75rem;font-weight:600}
  .on{background:#2a1800;color:#ffcc00}.off{background:#1a1a2a;color:var(--grey)}
  .hot{color:var(--warn)}.cold{color:var(--cool)}.ok{color:var(--accent)}
  table{width:100%;border-collapse:collapse;font-size:.85rem}
  th{text-align:left;color:var(--grey);padding:.4rem .6rem;border-bottom:1px solid #22224a}
  td{padding:.35rem .6rem;border-bottom:1px solid #16163a}
  .section-title{color:var(--grey);font-size:.75rem;text-transform:uppercase;
                 letter-spacing:.08em;margin:.8rem 0 .4rem}
  button{background:var(--accent);color:#000;border:0;padding:.4rem 1rem;
         border-radius:8px;cursor:pointer;font-weight:600;margin:.15rem}
  button.off-btn{background:#333;color:#ccc}
  button.cfg-btn{background:#2244aa;color:#fff}
  button.stop-btn{background:#ff3030;color:#fff}
  #chart-wrap{background:var(--card);border-radius:12px;padding:1rem;margin-bottom:1rem}
  canvas{width:100%!important;max-height:220px}
</style>
</head>
<body>
<div class="topbar">
  <h1>🦎 {{ site_title }}</h1>
  <div class="topbar-btns">
    <a href="/logs"><button class="cfg-btn" style="background:#444">📋 Logs</button></a>
    <a href="/config"><button class="cfg-btn">⚙️ Config</button></a>
    {% if auth_enabled %}<a href="/logout"><button class="off-btn">Logout</button></a>{% endif %}
  </div>
</div>

<div id="care-banner" style="display:none;background:linear-gradient(90deg,#1a3a7a,#2864c8);
     border:1px solid #50a0ff;border-radius:12px;padding:.9rem 1rem;margin-bottom:1rem;
     align-items:center;gap:1rem;flex-wrap:wrap">
  <div style="font-size:1.4rem">🩺</div>
  <div style="flex:1;min-width:200px">
    <div style="font-weight:700;color:#fff">Pet check-in needed</div>
    <div id="care-banner-msg" style="font-size:.85rem;color:#c8e0ff;margin-top:.2rem"></div>
  </div>
  <button onclick="document.querySelector('#care-btns button')?.scrollIntoView({behavior:'smooth',block:'center'})"
    style="background:#fff;color:#1a3a7a;border:0;padding:.5rem 1rem;border-radius:8px;
           font-weight:700;cursor:pointer">Log Care →</button>
</div>

<!-- Gecko + live sensor cards share a single grid so they pack side-by-side.
     display:contents makes the inner wrappers transparent so their children
     become direct grid items of the parent. -->
<div class="grid" style="margin-bottom:1rem">
  <div id="gecko-wrap"   style="display:none"></div>
  <div id="sensors"      style="display:contents"></div>
</div>

<div id="averages-wrap" style="display:none">
  <div class="section-title">📊 24h Averages</div>
  <div id="averages-cards" class="grid" style="margin-bottom:1.5rem"></div>
</div>

<div class="section-title">🌤 Outdoor Weather</div>
<div id="weather-wrap" style="display:none;margin-bottom:1.5rem">
  <div class="grid" id="weather-cards"></div>
  <div id="weather-chart-wrap" style="background:var(--card);border-radius:12px;padding:1rem;margin-top:.5rem">
    <div class="section-title">Outdoor Temp &amp; Humidity (last 24h)</div>
    <canvas id="weather-chart"></canvas>
  </div>
</div>
<div class="section-title">Relays &amp; Lights</div>
<div id="relays" class="grid"></div>
<div id="chart-wrap">
  <div class="section-title">Temperature &amp; Humidity (last 24 h)</div>
  <canvas id="chart"></canvas>
</div>

<div class="section-title">Camera</div>
<div id="camera-wrap" style="background:var(--card);border-radius:12px;padding:1rem;
     margin-bottom:1rem;display:none">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.5rem">
    <span id="cam-name" style="color:var(--accent);font-weight:600"></span>
    <span id="cam-ts" style="color:var(--grey);font-size:.8rem"></span>
    <button onclick="refreshSnap()">⟳ Refresh</button>
  </div>
  <img id="cam-img" src="" alt="snapshot"
       style="width:100%;border-radius:8px;max-height:360px;object-fit:contain;background:#000">
  <div id="cam-rtsp" style="margin-top:.5rem;font-size:.75rem;color:var(--grey)"></div>
</div>

<div class="section-title">🍽 Feeding Log</div>
<div style="background:var(--card);border-radius:12px;padding:1rem;margin-bottom:1rem">
  <div id="feed-btns" style="display:flex;flex-wrap:wrap;gap:.4rem;margin-bottom:.6rem">
    <span style="color:var(--grey);font-size:.85rem">Loading food items...</span>
  </div>
  <div style="display:flex;gap:.5rem;align-items:center">
    <input id="feed-note" type="text" placeholder="Optional note (e.g. 2 crickets, refused)..."
      style="flex:1;padding:.4rem .7rem;background:#0a0a20;border:1px solid #22224a;
             border-radius:8px;color:var(--text);font-size:.9rem">
    <button onclick="logCustomFeed()"
      style="background:#aa88ff;color:#000;white-space:nowrap;border:0">+ Custom</button>
  </div>
  <div id="feed-confirm" style="margin-top:.5rem;font-size:.85rem;color:var(--accent);min-height:1.4em"></div>
</div>

<div class="section-title">🩺 Care Log</div>
<div style="background:var(--card);border-radius:12px;padding:1rem;margin-bottom:1rem">
  <div id="care-btns" style="display:flex;flex-wrap:wrap;gap:.4rem;margin-bottom:.6rem">
    <span style="color:var(--grey);font-size:.85rem">Loading care items...</span>
  </div>
  <div style="display:flex;gap:.5rem;align-items:center">
    <input id="care-note" type="text" placeholder="Optional note (e.g. full clean, noticed shed)..."
      style="flex:1;padding:.4rem .7rem;background:#0a0a20;border:1px solid #22224a;
             border-radius:8px;color:var(--text);font-size:.9rem">
    <button onclick="logCustomCare()"
      style="background:#50a0ff;color:#000;white-space:nowrap;border:0">+ Custom</button>
  </div>
  <div id="care-confirm" style="margin-top:.5rem;font-size:.85rem;color:#50a0ff;min-height:1.4em"></div>
</div>

<div class="section-title" style="display:flex;justify-content:space-between;align-items:center">
  <span>Event Log</span>
  <button onclick="clearLog()"
    style="background:#333;color:#ccc;font-size:.75rem;padding:.25rem .7rem;
           border-radius:6px;border:1px solid #444;cursor:pointer;font-weight:400">
    🗑 Clear Log</button>
</div>
<table><thead><tr><th>Time</th><th>Category</th><th>Message</th></tr></thead>
<tbody id="events"></tbody></table>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script>
let chartInst = null;

let weatherChartInst = null;

async function loadWeather(){
  try {
    const r = await fetch('/api/weather/status');
    const d = await r.json();
    if(!d.enabled || !d.available){
      // Still show timelapse + system cards on their own if weather is off
      loadTimelapseCard(true);
      loadSystemStats();
      return;
    }
    document.getElementById('weather-wrap').style.display = 'block';
    document.getElementById('weather-cards').innerHTML = `
      <div class="card">
        <h3>${d.city || 'Outdoor'}</h3>
        <div style="display:flex;align-items:center;gap:.5rem">
          <img src="${d.icon_url}" style="width:50px;height:50px" alt="${d.description}">
          <div class="big ok">${d.temp_display}</div>
        </div>
        <div class="sub">${d.description}</div>
        <div class="sub">Feels like ${d.feels_like_display}</div>
      </div>
      <div class="card">
        <h3>Outdoor Humidity</h3>
        <div class="big" style="color:var(--cool)">${d.humidity}%</div>
        <div class="sub">Wind ${(d.wind_speed * 3.6).toFixed(1)} km/h</div>
        <div class="sub">${d.ts_local}</div>
      </div>`;
    loadTimelapseCard(false);
    loadSystemStats();
  } catch(e){ console.error('loadWeather error:', e); }
}

function geckoSvg(mood){
  // Cute side-view gecko. `currentColor` paints the body so we can recolor
  // by setting `color:` on the parent. Eye + mouth vary by mood.
  let mouth, eyeY = 30;
  if(mood === 'happy'){
    mouth = '<path d="M 160 42 Q 168 48, 176 42" stroke="#000" stroke-width="2" fill="none" stroke-linecap="round"/>';
  } else if(mood === 'neutral'){
    mouth = '<line x1="160" y1="44" x2="176" y2="44" stroke="#000" stroke-width="2" stroke-linecap="round"/>';
  } else {
    mouth = '<path d="M 160 46 Q 168 40, 176 46" stroke="#000" stroke-width="2" fill="none" stroke-linecap="round"/>';
    eyeY = 32;
  }
  return `
    <svg viewBox="0 0 200 110" style="width:100%;max-width:170px;display:block;margin:.3rem auto">
      <!-- Tail -->
      <path d="M 40 70 Q 8 60, 14 30 Q 18 18, 30 28 Q 26 50, 50 64 Z" fill="currentColor"/>
      <!-- Body -->
      <ellipse cx="100" cy="68" rx="55" ry="18" fill="currentColor"/>
      <!-- Back leg -->
      <ellipse cx="70" cy="86" rx="9" ry="13" fill="currentColor"/>
      <!-- Front leg -->
      <ellipse cx="135" cy="86" rx="9" ry="13" fill="currentColor"/>
      <!-- Toes -->
      <circle cx="63" cy="96" r="3" fill="currentColor"/>
      <circle cx="70" cy="98" r="3" fill="currentColor"/>
      <circle cx="77" cy="96" r="3" fill="currentColor"/>
      <circle cx="128" cy="96" r="3" fill="currentColor"/>
      <circle cx="135" cy="98" r="3" fill="currentColor"/>
      <circle cx="142" cy="96" r="3" fill="currentColor"/>
      <!-- Head -->
      <ellipse cx="160" cy="50" rx="28" ry="20" fill="currentColor"/>
      <!-- Spots -->
      <circle cx="85" cy="62" r="3" fill="rgba(0,0,0,.18)"/>
      <circle cx="105" cy="58" r="3" fill="rgba(0,0,0,.18)"/>
      <circle cx="125" cy="64" r="3" fill="rgba(0,0,0,.18)"/>
      <!-- Eye -->
      <circle cx="170" cy="${eyeY+12}" r="5" fill="#fff"/>
      <circle cx="171" cy="${eyeY+12}" r="2.6" fill="#000"/>
      <!-- Mouth -->
      ${mouth}
    </svg>`;
}

async function loadGecko(){
  try {
    const r = await fetch('/api/gecko/status');
    const d = await r.json();
    const wrap = document.getElementById('gecko-wrap');
    if(!d.enabled){
      wrap.style.display = 'none';
      wrap.innerHTML = '';
      return;
    }
    // 'contents' so the inner card flows into the shared parent grid.
    wrap.style.display = 'contents';
    const palette = {
      happy:   {color:'#00c878', label:'Happy',   bg:'#0a2818'},
      neutral: {color:'#ffcc00', label:'Meh',     bg:'#28220a'},
      upset:   {color:'#ff5028', label:'Upset',   bg:'#2a0e0a'},
      unknown: {color:'#7878a0', label:'No data', bg:'#14142a'},
    };
    const p = palette[d.mood] || palette.unknown;
    const reasons = (d.reasons || []).map(r=>{
      const dot = r.score === 2 ? '🟢' : r.score === 1 ? '🟡' : '🔴';
      return `<div style="font-size:.78rem;color:var(--grey);margin-top:.15rem">${dot} ${r.label}</div>`;
    }).join('');
    wrap.innerHTML = `
      <div class="card" style="background:${p.bg};border:1px solid ${p.color}33">
        <h3>🦎 Gecko Mood</h3>
        <div style="color:${p.color}">${geckoSvg(d.mood)}</div>
        <div style="text-align:center;color:${p.color};font-weight:700;
                    font-size:1.1rem;margin-top:.2rem">${p.label}</div>
        <div style="text-align:center;color:var(--grey);font-size:.8rem;
                    margin-top:.2rem">${d.summary || ''}</div>
        ${reasons ? `<div style="margin-top:.6rem;border-top:1px solid #22224a;padding-top:.4rem">${reasons}</div>` : ''}
      </div>`;
  } catch(e){ console.error('loadGecko error:', e); }
}

function humidityArc(pct, thresholds){
  // Half-moon gauge from (10,50) to (90,50), radius 40, center (50,50).
  const p = Math.max(0, Math.min(100, pct));
  const theta = Math.PI * (1 - p/100);   // 180° → 0°
  const x = 50 + 40 * Math.cos(theta);
  const y = 50 - 40 * Math.sin(theta);
  const t = thresholds || {low:50, high:80, color_low:'#2878ff', color_good:'#14c83c', color_high:'#dc3c14'};
  const color = p < t.low ? t.color_low : (p > t.high ? t.color_high : t.color_good);
  const fg = p > 0
    ? `<path d="M 10 50 A 40 40 0 0 1 ${x.toFixed(2)} ${y.toFixed(2)}"
            stroke="${color}" stroke-width="8" fill="none" stroke-linecap="round"/>`
    : '';
  return `
    <svg viewBox="0 0 100 60" style="width:100%;max-width:180px;display:block;margin:.4rem auto 0">
      <path d="M 10 50 A 40 40 0 0 1 90 50"
            stroke="#22224a" stroke-width="8" fill="none" stroke-linecap="round"/>
      ${fg}
      <text x="50" y="46" text-anchor="middle" fill="#e0e0f0"
            font-size="16" font-weight="700" font-family="system-ui,sans-serif">${p.toFixed(1)}%</text>
    </svg>`;
}

async function loadSensorAverages(){
  try {
    const r = await fetch('/api/sensors/averages?hours=24');
    const d = await r.json();
    if(!d.sensors || !d.sensors.length){
      document.getElementById('averages-wrap').style.display = 'none';
      return;
    }
    document.getElementById('averages-wrap').style.display = 'block';
    const th = d.humidity_thresholds;
    document.getElementById('averages-cards').innerHTML = d.sensors.map(s=>`
      <div class="card">
        <h3>${s.name} — Avg</h3>
        <div style="display:flex;justify-content:space-between;align-items:baseline">
          <span style="color:var(--grey);font-size:.85rem">Temp</span>
          <span class="big ok" style="font-size:1.6rem">${s.avg_temp_display}</span>
        </div>
        <div style="color:var(--grey);font-size:.8rem;margin-top:.6rem">Humidity</div>
        ${humidityArc(s.avg_humidity, th)}
        <div style="text-align:center;color:var(--grey);font-size:.7rem;margin-top:.2rem">
          ${s.count} samples · 24h
        </div>
      </div>`).join('');
  } catch(e){ console.error('loadSensorAverages error:', e); }
}

async function loadSystemStats(){
  try {
    const r = await fetch('/api/system/stats');
    const d = await r.json();
    document.getElementById('weather-wrap').style.display = 'block';
    const tempColor = (d.cpu_temp_c !== null && d.cpu_temp_c >= 70) ? 'var(--warn)'
                    : (d.cpu_temp_c !== null && d.cpu_temp_c >= 60) ? '#ffcc00'
                    : 'var(--accent)';
    const cpuColor  = (d.cpu_percent !== null && d.cpu_percent >= 80) ? 'var(--warn)'
                    : (d.cpu_percent !== null && d.cpu_percent >= 50) ? '#ffcc00'
                    : 'var(--accent)';
    const ramColor  = (d.ram_percent !== null && d.ram_percent >= 85) ? 'var(--warn)'
                    : (d.ram_percent !== null && d.ram_percent >= 70) ? '#ffcc00'
                    : 'var(--cool)';
    const diskColor = (d.disk_percent !== null && d.disk_percent >= 90) ? 'var(--warn)'
                    : 'var(--text)';
    const row = (label, value, color) => `
        <div style="display:flex;justify-content:space-between;margin-bottom:.3rem">
          <span style="color:var(--grey);font-size:.85rem">${label}</span>
          <span style="color:${color};font-weight:700">${value}</span>
        </div>`;
    const html = `
      <div class="card">
        <h3>🖥 System</h3>
        ${row('CPU Temp', d.cpu_temp_display, tempColor)}
        ${row('CPU', d.cpu_percent !== null ? d.cpu_percent.toFixed(1) + '%' : 'N/A', cpuColor)}
        ${row('RAM', d.ram_percent !== null ? d.ram_percent.toFixed(1) + '%' : 'N/A', ramColor)}
        ${row('Free Space', d.free_display, diskColor)}
        ${row('Uptime', d.uptime_display, 'var(--text)')}
      </div>`;
    document.getElementById('weather-cards').insertAdjacentHTML('beforeend', html);
  } catch(e){ console.error('loadSystemStats error:', e); }
}

async function loadTimelapseCard(standalone){
  try {
    const r = await fetch('/api/timelapse/frames');
    const d = await r.json();
    const count = (d.frames || []).length;
    if(standalone){
      document.getElementById('weather-wrap').style.display = 'block';
    }
    const html = `
      <div class="card">
        <h3>🎬 Timelapse</h3>
        <div class="big ok">${count}</div>
        <div class="sub">${count === 1 ? 'frame' : 'frames'}</div>
        <div class="sub" style="margin-top:.5rem">
          <a href="/timelapse" style="color:var(--accent);text-decoration:none;font-weight:600">View →</a>
        </div>
      </div>`;
    document.getElementById('weather-cards').insertAdjacentHTML('beforeend', html);
  } catch(e){ console.error('loadTimelapseCard error:', e); }
}

async function loadWeatherChart(){
  try {
    const r = await fetch('/api/weather/history?hours=24');
    const rows = await r.json();
    if(!rows.length) return;
    const labels = rows.map(r=>r.ts_local);
    const temps  = rows.map(r=>r.temp_display_val);
    const hums   = rows.map(r=>r.humidity);
    if(weatherChartInst) weatherChartInst.destroy();
    const ctx = document.getElementById('weather-chart').getContext('2d');
    weatherChartInst = new Chart(ctx, {
      type:'line',
      data:{ labels, datasets:[
        {label:'Outdoor Temp', data:temps, borderColor:'#ffaa00',
         tension:.3, yAxisID:'temp', pointRadius:0},
        {label:'Outdoor Humidity', data:hums, borderColor:'#50a0ff',
         borderDash:[4,4], tension:.3, yAxisID:'hum', pointRadius:0},
      ]},
      options:{
        animation:false,
        plugins:{legend:{labels:{color:'#7878a0',font:{size:11}}}},
        scales:{
          x:{ticks:{color:'#7878a0',maxTicksLimit:8},grid:{color:'#16163a'}},
          temp:{position:'left', ticks:{color:'#ffaa00'}, grid:{color:'#16163a'},
                title:{display:true,text:'Temp',color:'#7878a0'}},
          hum:{position:'right', ticks:{color:'#50a0ff'}, grid:{drawOnChartArea:false},
               title:{display:true,text:'Humidity %',color:'#7878a0'}}
        }
      }
    });
  } catch(e){ console.error('loadWeatherChart error:', e); }
}

async function refresh(){
  const r = await fetch('/api/status');
  const d = await r.json();

  document.getElementById('sensors').innerHTML = d.sensors.map(s=>`
    <div class="card">
      <h3>${s.name}</h3>
      <div class="big ${s.temp_class}">${s.temp_display}</div>
      <div class="sub">${s.humidity.toFixed(1)}% RH</div>
      <div class="sub" style="margin-top:.5rem">${s.ts_local}</div>
    </div>`).join('');

  document.getElementById('relays').innerHTML = d.relays.map(r=>`
    <div class="card">
      <h3>${r.name}</h3>
      <span class="badge ${r.is_on?'on':'off'}">${r.is_on?'ON':'OFF'}</span>
      ${r.in_cooldown?'<span class="badge off" style="margin-left:.3rem">COOLING</span>':''}
      <div style="margin-top:.6rem">
        <button onclick="relayCmd('${r.name}','on')">Force ON</button>
        <button class="off-btn" onclick="relayCmd('${r.name}','off')">Force OFF</button>
      </div>
    </div>`).join('') +
    d.lights.map(l=>`
    <div class="card">
      <h3>${l.name}</h3>
      <span class="badge ${l.state?'on':'off'}">${l.state?'ON':'OFF'}</span>
      <div class="sub">${l.on_time} – ${l.off_time}</div>
      <div style="margin-top:.6rem">
        <button onclick="lightCmd('${l.name}','on')">ON</button>
        <button class="off-btn" onclick="lightCmd('${l.name}','off')">OFF</button>
      </div>
    </div>`).join('');

  const catColors = {
    startup:'#00c878', shutdown:'#ff5028', config:'#50a0ff',
    relay:'#ffcc00',   light:'#ffaa00',    camera:'#aa88ff',
    feeding:'#ff88cc', care:'#50a0ff',     gecko:'#4ecdc4',
    error:'#ff3030'
  };
  document.getElementById('events').innerHTML = d.recent_events.map(e=>`
    <tr>
      <td>${e.ts_local}</td>
      <td><span class="badge" style="background:${catColors[e.category]||'#333'};color:#000">
          ${e.category}</span></td>
      <td>${e.message}</td>
    </tr>`).join('');
}

async function relayCmd(name,cmd){
  await fetch(`/api/relay/${encodeURIComponent(name)}/${cmd}`,{method:'POST'});
  refresh();
}
async function lightCmd(name,cmd){
  await fetch(`/api/light/${encodeURIComponent(name)}/${cmd}`,{method:'POST'});
  refresh();
}
async function stopPython(){
  if(!confirm('Stop the Python process and return to terminal?')) return;
  const btn=document.getElementById('shutdown-btn');
  btn.textContent='⏳ Stopping...'; btn.disabled=true;
  try{ await fetch('/api/shutdown',{method:'POST'}); }catch(e){}
  btn.textContent='✅ Stopped';
}
async function loadCareItems(){
  try {
    const r = await fetch('/api/care/items');
    const d = await r.json();
    const container = document.getElementById('care-btns');
    if(!container){ console.error('care-btns element not found'); return; }
    container.innerHTML = d.items.map(item=>`
      <button onclick="logCare(this.dataset.item)"
              data-item="${item}"
        style="background:#0a1a3a;color:#50a0ff;border:1px solid #50a0ff;
               border-radius:8px;padding:.3rem .8rem;cursor:pointer;font-size:.85rem">
        🩺 ${item}
      </button>`).join('');
  } catch(e){ console.error('loadCareItems error:', e); }
}

async function logCare(item){
  const note = document.getElementById('care-note').value.trim();
  const r = await fetch('/api/care/log', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({item, note})
  });
  const d = await r.json();
  const confirm = document.getElementById('care-confirm');
  confirm.textContent = d.ok ? `✅ Logged: ${d.message}` : `❌ ${d.error}`;
  document.getElementById('care-note').value = '';
  setTimeout(()=>{ confirm.textContent=''; }, 4000);
  refresh();
  loadCareStatus();
}

async function loadCareStatus(){
  try {
    const r = await fetch('/api/care/status');
    const d = await r.json();
    const banner = document.getElementById('care-banner');
    if(!d.enabled || !d.due){
      banner.style.display = 'none';
      return;
    }
    const msg = d.last_ts
      ? `You haven't checked on your pet in ${d.ago} (last ${d.last_category}: ${d.last_message} · ${d.last_ts}). Please check in and log a care or feeding item.`
      : `No check-in has been logged yet. Please log a care or feeding item.`;
    document.getElementById('care-banner-msg').textContent = msg;
    banner.style.display = 'flex';
  } catch(e){ console.error('loadCareStatus error:', e); }
}

async function logCustomCare(){
  const note = document.getElementById('care-note').value.trim();
  if(!note){ alert('Enter a care activity in the note field.'); return; }
  await logCare(note);
}

async function loadFeedingItems(){
  try {
    const r = await fetch('/api/feeding/items');
    const d = await r.json();
    const container = document.getElementById('feed-btns');
    if(!container){ console.error('feed-btns element not found'); return; }
    container.innerHTML = d.items.map(item=>`
      <button onclick="logFeed(this.dataset.item)"
              data-item="${item}"
        style="background:#2a1a3a;color:#ff88cc;border:1px solid #ff88cc;
               border-radius:8px;padding:.3rem .8rem;cursor:pointer;font-size:.85rem">
        🍽 ${item}
      </button>`).join('');
  } catch(e){ console.error('loadFeedingItems error:', e); }
}

async function logFeed(item){
  const note = document.getElementById('feed-note').value.trim();
  const r = await fetch('/api/feeding/log', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({item, note})
  });
  const d = await r.json();
  const confirm = document.getElementById('feed-confirm');
  confirm.textContent = d.ok ? `✅ Logged: ${d.message}` : `❌ ${d.error}`;
  document.getElementById('feed-note').value = '';
  setTimeout(()=>{ confirm.textContent=''; }, 4000);
  refresh();
  loadCareStatus();
}

async function logCustomFeed(){
  const note = document.getElementById('feed-note').value.trim();
  if(!note){ alert('Enter a food item in the note field.'); return; }
  await logFeed(note);
}

async function clearLog(){
  if(!confirm('Clear all event log entries? This cannot be undone.')) return;
  await fetch('/api/log/clear', {method:'POST'});
  refresh();
}

async function refreshSnap(){
  document.getElementById('cam-img').src='/api/camera/snapshot?t='+Date.now();
}
async function loadCamera(){
  const r=await fetch('/api/camera/status');
  const d=await r.json();
  if(!d.enabled) return;
  document.getElementById('camera-wrap').style.display='block';
  document.getElementById('cam-name').textContent=d.name;
  document.getElementById('cam-ts').textContent='Last: '+d.last_snapshot;
  document.getElementById('cam-rtsp').textContent=
    d.rtsp_uri?'RTSP: '+d.rtsp_uri.replace(/:[^@]+@/,':***@'):'';
  refreshSnap();
}
async function loadChart(){
  const r=await fetch('/api/history?hours=24');
  const rows=await r.json();
  const sensors={};
  rows.forEach(row=>{
    if(!sensors[row.sensor_name]) sensors[row.sensor_name]={labels:[],temp:[],hum:[]};
    sensors[row.sensor_name].labels.push(row.ts_local);
    sensors[row.sensor_name].temp.push(row.temp_display_val);
    sensors[row.sensor_name].hum.push(row.humidity);
  });
  const colors=['#00c878','#50a0ff','#ff5028','#ffcc00'];
  const datasets=[];
  let ci=0;
  for(const [name,data] of Object.entries(sensors)){
    datasets.push({label:`${name} Temp`,data:data.temp,borderColor:colors[ci],tension:.3,yAxisID:'temp',pointRadius:0});
    datasets.push({label:`${name} Hum`,data:data.hum,borderColor:colors[ci],borderDash:[4,4],tension:.3,yAxisID:'hum',pointRadius:0});
    ci=(ci+1)%colors.length;
  }
  const labels=rows.length?Object.values(sensors)[0].labels:[];
  if(chartInst) chartInst.destroy();
  const ctx=document.getElementById('chart').getContext('2d');
  chartInst=new Chart(ctx,{
    type:'line',data:{labels,datasets},
    options:{
      animation:false,
      plugins:{legend:{labels:{color:'#7878a0',font:{size:11}}}},
      scales:{
        x:{ticks:{color:'#7878a0',maxTicksLimit:8},grid:{color:'#16163a'}},
        temp:{position:'left',ticks:{color:'#00c878'},grid:{color:'#16163a'},
              title:{display:true,text:'Temp',color:'#7878a0'}},
        hum:{position:'right',ticks:{color:'#50a0ff'},grid:{drawOnChartArea:false},
             title:{display:true,text:'Humidity %',color:'#7878a0'}}
      }
    }
  });
}

refresh(); loadCamera(); loadChart(); loadWeather(); loadWeatherChart();
loadFeedingItems(); loadCareItems(); loadSensorAverages(); loadCareStatus();
loadGecko();
setInterval(refresh, 30000);
setInterval(loadCamera, 30000);
setInterval(loadWeather, 600000);
setInterval(loadWeatherChart, 600000);
setInterval(loadChart, 120000);
setInterval(loadSensorAverages, 300000);
setInterval(loadCareStatus, 60000);
setInterval(loadGecko, 30000);
setInterval(()=>{
  // Refresh just the system stats card by removing & re-adding it
  const cards = document.getElementById('weather-cards');
  if(!cards) return;
  const sysCard = Array.from(cards.children).find(c => c.querySelector('h3')?.textContent.includes('System'));
  if(sysCard) sysCard.remove();
  loadSystemStats();
}, 30000);
</script>
</body>
</html>"""


@app.route("/")
@_auth_required
def index():
    from flask import render_template_string
    title = _config.get("web", "site_title", fallback="Terrarium Monitor") if _config else "Terrarium Monitor"
    return render_template_string(DASHBOARD_HTML,
                                  auth_enabled=_auth_enabled(),
                                  site_title=title)


# ------------------------------------------------------------------
# API routes
# ------------------------------------------------------------------

@app.route("/api/status")
@_auth_required
def api_status():
    latest    = _data_logger.get_latest_readings() if _data_logger else []
    temp_unit = _config.get("display", "temp_unit", fallback="F").upper() if _config else "F"
    sensors_out = []
    for row in latest:
        tc = row["temp_c"]
        td = tc * 9/5 + 32 if temp_unit == "F" else tc
        unit = "°F" if temp_unit == "F" else "°C"
        try:
            hi = _config.getfloat("fan", "temp_threshold_high", fallback=30)
            lo = _config.getfloat("fan", "temp_threshold_low",  fallback=20)
            cls = "hot" if tc > hi else ("cold" if tc < lo else "ok")
        except Exception:
            cls = "ok"
        sensors_out.append({
            "name": row["sensor_name"], "temp_c": tc,
            "temp_display_val": round(td, 1),
            "temp_display": f"{td:.1f}{unit}",
            "humidity": row["humidity"], "temp_class": cls,
            "ts_local": _utc_to_local(row["ts"]),
        })

    relays_out = []
    for relay in [_mister, _fan]:
        if relay:
            relays_out.append({"name": relay.name, "is_on": relay.is_on,
                               "in_cooldown": relay.in_cooldown})

    lights_out = _lighting.status() if _lighting else []
    events = _data_logger.get_all_events(hours=24) if _data_logger else []
    for e in events:
        e["ts_local"] = _utc_to_local(e["ts"])

    return jsonify({"sensors": sensors_out, "relays": relays_out,
                    "lights": lights_out, "recent_events": events[:50],
                    "now": datetime.now().strftime("%Y-%m-%d %H:%M")})


@app.route("/api/history")
@_auth_required
def api_history():
    hours  = request.args.get("hours", 24, type=int)
    sensor = request.args.get("sensor", None)
    rows   = _data_logger.get_readings(hours=hours, sensor_name=sensor) if _data_logger else []
    temp_unit = _config.get("display", "temp_unit", fallback="F").upper() if _config else "F"
    for row in rows:
        tc = row["temp_c"]
        row["temp_display_val"] = round(tc * 9/5 + 32 if temp_unit == "F" else tc, 1)
        row["ts_local"] = row["ts"].replace("T"," ")[:16]
    return jsonify(rows)


@app.route("/api/relay/<name>/<cmd>", methods=["POST"])
@_auth_required
def api_relay(name, cmd):
    relay = next((r for r in [_mister, _fan] if r and r.name.lower()==name.lower()), None)
    if not relay:
        return jsonify({"error": "unknown relay"}), 404
    if cmd == "on":  relay.trigger(reason="manual override via web UI")
    elif cmd == "off": relay.force_off()
    return jsonify({"ok": True})


@app.route("/api/light/<name>/<cmd>", methods=["POST"])
@_auth_required
def api_light(name, cmd):
    if not _lighting:
        return jsonify({"error": "lighting not configured"}), 404
    plug = next((p for p in _lighting.plugs if p.name.lower()==name.lower()), None)
    if not plug:
        return jsonify({"error": "unknown plug"}), 404
    if cmd == "on":  plug.turn_on()
    elif cmd == "off": plug.turn_off()
    return jsonify({"ok": True})


@app.route("/api/camera/snapshot")
@_auth_required
def api_camera_snapshot():
    if not _camera or not _camera.enabled:
        return "Camera not configured", 404
    p = _camera.snapshot_path
    if not p.exists():
        return "No snapshot yet", 404
    from flask import send_file
    resp = send_file(str(p.resolve()), mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/camera/status")
@_auth_required
def api_camera_status():
    if not _camera:
        return jsonify({"enabled": False})
    return jsonify(_camera.status())


@app.route("/api/camera/refresh", methods=["POST"])
@_auth_required
def api_camera_refresh():
    if not _camera or not _camera.enabled:
        return jsonify({"error": "camera not configured"}), 404
    ok = _camera.capture_snapshot()
    return jsonify({"ok": ok})


@app.route("/api/weather/status")
@_auth_required
def api_weather_status():
    if not _weather:
        return jsonify({"enabled": False})
    return jsonify(_weather.status())


@app.route("/api/weather/history")
@_auth_required
def api_weather_history():
    hours = request.args.get("hours", 24, type=int)
    rows  = _data_logger.get_weather_readings(hours=hours) if _data_logger else []
    if _config:
        temp_unit = _config.get("weather", "temp_unit", fallback=None) or \
                    _config.get("display", "temp_unit", fallback="F")
    else:
        temp_unit = "F"
    temp_unit = temp_unit.upper()
    for row in rows:
        tc = row["temp_c"]
        row["temp_display_val"] = round(tc * 9/5 + 32 if temp_unit == "F" else tc, 1)
        row["ts_local"] = _utc_to_local(row["ts"])
    return jsonify(rows)


@app.route("/api/care/items")
@_auth_required
def api_care_items():
    items = _config.get("care", "care_items", fallback="Cleaning, Misting, Substrate Change, Water Change, Vet Visit")
    return jsonify({"items": [i.strip() for i in items.split(",") if i.strip()]})


@app.route("/api/care/status")
@_auth_required
def api_care_status():
    """Tell the dashboard whether a check-in (care OR feeding) is overdue."""
    threshold = _config.getfloat("care", "care_reminder_hours", fallback=24.0) if _config else 24.0
    if threshold <= 0 or not _data_logger:
        return jsonify({"enabled": False})
    candidates = [
        _data_logger.get_last_event_by_category("care"),
        _data_logger.get_last_event_by_category("feeding"),
    ]
    candidates = [c for c in candidates if c]
    last = None
    for c in candidates:
        try:
            ts = datetime.fromisoformat(c["ts"])
        except Exception:
            continue
        if last is None or ts > datetime.fromisoformat(last["ts"]):
            last = c
    now = datetime.utcnow()
    if last:
        last_ts_dt = datetime.fromisoformat(last["ts"])
        hours_since = (now - last_ts_dt).total_seconds() / 3600
    else:
        hours_since = None
    due = (hours_since is None) or (hours_since >= threshold)
    if hours_since is None:
        ago = "never"
    elif hours_since < 1:
        ago = f"{int(hours_since * 60)}m ago"
    elif hours_since < 48:
        ago = f"{int(hours_since)}h ago"
    else:
        ago = f"{int(hours_since // 24)}d ago"
    return jsonify({
        "enabled": True,
        "threshold_hours": threshold,
        "hours_since": round(hours_since, 1) if hours_since is not None else None,
        "ago": ago,
        "last_ts": _utc_to_local(last["ts"]) if last else None,
        "last_category": last["category"] if last else None,
        "last_message": last["message"] if last else None,
        "due": due,
    })


@app.route("/api/care/log", methods=["POST"])
@_auth_required
def api_care_log():
    data = request.get_json(silent=True) or {}
    item = data.get("item", "").strip()
    note = data.get("note", "").strip()
    if not item:
        return jsonify({"error": "no item specified"}), 400
    msg = item + (f" — {note}" if note else "")
    if _data_logger:
        _data_logger.log_system_event("care", msg)
    return jsonify({"ok": True, "message": msg})


@app.route("/api/feeding/items")
@_auth_required
def api_feeding_items():
    items = _config.get("feeding", "food_items", fallback="Cricket, Waxworm, Paste, Water")
    return jsonify({"items": [i.strip() for i in items.split(",") if i.strip()]})


@app.route("/api/feeding/log", methods=["POST"])
@_auth_required
def api_feeding_log():
    data = request.get_json(silent=True) or {}
    item = data.get("item", "").strip()
    note = data.get("note", "").strip()
    if not item:
        return jsonify({"error": "no item specified"}), 400
    msg = item + (f" — {note}" if note else "")
    if _data_logger:
        _data_logger.log_system_event("feeding", msg)
    return jsonify({"ok": True, "message": msg})


@app.route("/api/log/clear", methods=["POST"])
@_auth_required
def api_log_clear():
    """Clear all system events and relay events from the database."""
    try:
        from data_logger import _conn
        with _conn() as con:
            con.execute("DELETE FROM system_events")
            con.execute("DELETE FROM relay_events")
        if _data_logger:
            _data_logger.log_system_event("config", "Event log cleared via web UI")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


_cpu_last_sample = None  # (total, idle) from previous /proc/stat read

def _read_cpu_sample():
    """Read aggregate CPU jiffies from /proc/stat. Returns (total, idle) or None."""
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        # parts[0] == 'cpu', remaining are: user nice system idle iowait irq softirq steal ...
        nums = [int(x) for x in parts[1:]]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)  # idle + iowait
        total = sum(nums)
        return total, idle
    except Exception:
        return None


def _format_uptime(seconds: float) -> str:
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    mins, _ = divmod(s, 60)
    if days:  return f"{days}d {hours}h"
    if hours: return f"{hours}h {mins}m"
    return f"{mins}m"


def _rgb_csv_to_hex(s: str, fallback: str) -> str:
    """Convert 'R,G,B' from config into '#rrggbb'."""
    try:
        r, g, b = [int(x.strip()) for x in s.split(",")[:3]]
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return fallback


@app.route("/api/gecko/status")
@_auth_required
def api_gecko_status():
    """Combined mood score from temp, humidity, and check-in recency.
    Logic lives in gecko_mood.compute_mood() so the OLED uses the same scoring."""
    from gecko_mood import compute_mood
    return jsonify(compute_mood(_config, _data_logger))


@app.route("/api/sensors/averages")
@_auth_required
def api_sensors_averages():
    """Return per-sensor average temp and humidity over the past N hours."""
    hours = request.args.get("hours", 24, type=int)
    rows  = _data_logger.get_readings(hours=hours) if _data_logger else []
    temp_unit = _config.get("display", "temp_unit", fallback="F").upper() if _config else "F"
    unit = "°F" if temp_unit == "F" else "°C"

    # Humidity thresholds & colors mirror the OLED display config
    hum_low  = _config.getfloat("display", "humidity_display_low",  fallback=50.0) if _config else 50.0
    hum_high = _config.getfloat("display", "humidity_display_high", fallback=80.0) if _config else 80.0
    col_low  = _rgb_csv_to_hex(_config.get("display", "color_hum_low",  fallback="40,120,255") if _config else "", "#2878ff")
    col_good = _rgb_csv_to_hex(_config.get("display", "color_hum_good", fallback="20,200,60")  if _config else "", "#14c83c")
    col_high = _rgb_csv_to_hex(_config.get("display", "color_hum_high", fallback="220,60,20")  if _config else "", "#dc3c14")

    buckets = {}
    for r in rows:
        b = buckets.setdefault(r["sensor_name"], {"temp_c": [], "humidity": []})
        b["temp_c"].append(r["temp_c"])
        b["humidity"].append(r["humidity"])

    out = []
    for name, b in buckets.items():
        if not b["temp_c"]:
            continue
        avg_c = sum(b["temp_c"]) / len(b["temp_c"])
        avg_h = sum(b["humidity"]) / len(b["humidity"])
        td = avg_c * 9/5 + 32 if temp_unit == "F" else avg_c
        out.append({
            "name": name,
            "avg_temp_c": round(avg_c, 1),
            "avg_temp_display": f"{td:.1f}{unit}",
            "avg_humidity": round(avg_h, 1),
            "count": len(b["temp_c"]),
        })
    return jsonify({
        "hours": hours,
        "sensors": out,
        "humidity_thresholds": {
            "low": hum_low, "high": hum_high,
            "color_low": col_low, "color_good": col_good, "color_high": col_high,
        },
    })


@app.route("/api/system/stats")
@_auth_required
def api_system_stats():
    """Return CPU temp, CPU %, RAM %, free disk, and uptime."""
    import shutil
    stats = {
        "cpu_temp_c": None, "cpu_temp_display": "N/A",
        "cpu_percent": None,
        "ram_percent": None,
        "free_gb": None, "free_display": "N/A",
        "disk_percent": None,
        "uptime_display": "N/A",
    }
    # CPU temperature (Raspberry Pi / Linux thermal zone)
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            tc = int(f.read().strip()) / 1000.0
        stats["cpu_temp_c"] = round(tc, 1)
        temp_unit = _config.get("display", "temp_unit", fallback="F").upper() if _config else "F"
        if temp_unit == "F":
            stats["cpu_temp_display"] = f"{tc * 9/5 + 32:.1f}°F"
        else:
            stats["cpu_temp_display"] = f"{tc:.1f}°C"
    except Exception:
        pass
    # Free disk space on root volume
    try:
        du = shutil.disk_usage("/")
        free_gb = du.free / (1024 ** 3)
        stats["free_gb"] = round(free_gb, 1)
        stats["free_display"] = f"{free_gb:.1f} GB"
        stats["disk_percent"] = round(du.used / du.total * 100, 1)
    except Exception:
        pass
    # RAM usage from /proc/meminfo
    try:
        meminfo = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                meminfo[key.strip()] = rest.strip()
        total_kb = int(meminfo["MemTotal"].split()[0])
        avail_kb = int(meminfo.get("MemAvailable", meminfo.get("MemFree", "0 kB")).split()[0])
        stats["ram_percent"] = round((total_kb - avail_kb) / total_kb * 100, 1)
    except Exception:
        pass
    # CPU % — compute delta vs last sample
    global _cpu_last_sample
    sample = _read_cpu_sample()
    if sample and _cpu_last_sample:
        d_total = sample[0] - _cpu_last_sample[0]
        d_idle  = sample[1] - _cpu_last_sample[1]
        if d_total > 0:
            stats["cpu_percent"] = round((1 - d_idle / d_total) * 100, 1)
    if sample:
        _cpu_last_sample = sample
    # Uptime
    try:
        with open("/proc/uptime") as f:
            stats["uptime_display"] = _format_uptime(float(f.read().split()[0]))
    except Exception:
        pass
    return jsonify(stats)


LOGS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Event Log – {{ site_title }}</title>
<style>
  :root{--bg:#0a0a14;--card:#14142a;--accent:#00c878;--text:#e0e0f0;--grey:#7878a0;--warn:#ff5028}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:system-ui,sans-serif;padding:1rem}
  h1{color:var(--accent);margin-bottom:.25rem;font-size:1.3rem}
  .nav{margin-bottom:1rem}
  .nav a{color:var(--accent);text-decoration:none;margin-right:1rem;font-size:.9rem}
  .toolbar{background:var(--card);border-radius:12px;padding:.8rem 1rem;margin-bottom:1rem}
  .row{display:flex;gap:.4rem;flex-wrap:wrap;align-items:center;margin-bottom:.6rem}
  .row:last-child{margin-bottom:0}
  .label{color:var(--grey);font-size:.8rem;text-transform:uppercase;letter-spacing:.06em;
         margin-right:.3rem;min-width:55px}
  .pill{background:#1a1a2a;color:var(--grey);border:1px solid #22224a;border-radius:99px;
        padding:.25rem .8rem;font-size:.8rem;cursor:pointer;font-family:inherit}
  .pill:hover{border-color:var(--accent);color:var(--text)}
  .pill.active{background:var(--accent);color:#000;border-color:var(--accent);font-weight:600}
  input[type=text]{flex:1;min-width:140px;padding:.35rem .7rem;background:#0a0a20;
                   border:1px solid #22224a;border-radius:8px;color:var(--text);font-size:.85rem}
  input[type=text]:focus{outline:none;border-color:var(--accent)}
  #count{color:var(--grey);font-size:.8rem;margin-left:auto}
  table{width:100%;border-collapse:collapse;font-size:.85rem;background:var(--card);
        border-radius:12px;overflow:hidden}
  th{text-align:left;color:var(--grey);padding:.6rem .8rem;
     border-bottom:1px solid #22224a;font-size:.75rem;text-transform:uppercase;letter-spacing:.05em}
  td{padding:.45rem .8rem;border-bottom:1px solid #16163a;vertical-align:top}
  tr:last-child td{border-bottom:none}
  td.ts{white-space:nowrap;color:var(--grey);font-variant-numeric:tabular-nums}
  td.msg{word-break:break-word}
  .badge{display:inline-block;padding:.15rem .55rem;border-radius:99px;font-size:.7rem;
         font-weight:600;color:#000}
  .empty{text-align:center;color:var(--grey);padding:2rem;font-size:.9rem}
</style>
</head>
<body>
<h1>📋 Event Log</h1>
<div class="nav">
  <a href="/">← Dashboard</a>
</div>

<div class="toolbar">
  <div class="row">
    <span class="label">Range</span>
    <button class="pill" data-hours="1">1h</button>
    <button class="pill" data-hours="6">6h</button>
    <button class="pill active" data-hours="24">24h</button>
    <button class="pill" data-hours="168">7d</button>
    <button class="pill" data-hours="720">30d</button>
    <input type="text" id="search" placeholder="Search messages...">
    <span id="count"></span>
  </div>
  <div class="row">
    <span class="label">Filter</span>
    <button class="pill active" data-cat="">All</button>
    <button class="pill" data-cat="startup">startup</button>
    <button class="pill" data-cat="shutdown">shutdown</button>
    <button class="pill" data-cat="config">config</button>
    <button class="pill" data-cat="relay">relay</button>
    <button class="pill" data-cat="light">light</button>
    <button class="pill" data-cat="camera">camera</button>
    <button class="pill" data-cat="feeding">feeding</button>
    <button class="pill" data-cat="care">care</button>
    <button class="pill" data-cat="gecko">gecko</button>
    <button class="pill" data-cat="error">error</button>
  </div>
</div>

<div id="results"></div>

<script>
const catColors = {
  startup:'#00c878', shutdown:'#ff5028', config:'#50a0ff',
  relay:'#ffcc00',   light:'#ffaa00',    camera:'#aa88ff',
  feeding:'#ff88cc', care:'#50a0ff',     gecko:'#4ecdc4',
  error:'#ff3030'
};
let selectedCats = new Set();  // empty = all
let hours = 24;
let searchTerm = '';

async function loadLogs(){
  const params = new URLSearchParams({hours: hours});
  if(selectedCats.size) params.set('categories', [...selectedCats].join(','));
  if(searchTerm) params.set('q', searchTerm);
  const r = await fetch('/api/logs?' + params);
  const d = await r.json();
  document.getElementById('count').textContent = d.count + ' event' + (d.count===1?'':'s');
  const container = document.getElementById('results');
  if(!d.events.length){
    container.innerHTML = '<div class="empty">No events match the current filter.</div>';
    return;
  }
  container.innerHTML = `
    <table>
      <thead><tr><th style="width:140px">Time</th><th style="width:90px">Category</th><th>Message</th></tr></thead>
      <tbody>
        ${d.events.map(e=>`
          <tr>
            <td class="ts">${e.ts_local}</td>
            <td><span class="badge" style="background:${catColors[e.category]||'#444'};color:#000">${e.category}</span></td>
            <td class="msg">${escapeHtml(e.message)}</td>
          </tr>`).join('')}
      </tbody>
    </table>`;
}

function escapeHtml(s){
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// Range pills (single-select)
document.querySelectorAll('.pill[data-hours]').forEach(btn=>{
  btn.addEventListener('click', ()=>{
    document.querySelectorAll('.pill[data-hours]').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    hours = parseInt(btn.dataset.hours);
    loadLogs();
  });
});

// Category pills (multi-select; "All" clears the set)
document.querySelectorAll('.pill[data-cat]').forEach(btn=>{
  btn.addEventListener('click', ()=>{
    const cat = btn.dataset.cat;
    if(cat === ''){
      selectedCats.clear();
      document.querySelectorAll('.pill[data-cat]').forEach(b=>b.classList.remove('active'));
      btn.classList.add('active');
    } else {
      document.querySelector('.pill[data-cat=""]').classList.remove('active');
      if(selectedCats.has(cat)){
        selectedCats.delete(cat);
        btn.classList.remove('active');
      } else {
        selectedCats.add(cat);
        btn.classList.add('active');
      }
      if(selectedCats.size === 0){
        document.querySelector('.pill[data-cat=""]').classList.add('active');
      }
    }
    loadLogs();
  });
});

// Search input (debounced)
let searchTimer = null;
document.getElementById('search').addEventListener('input', (ev)=>{
  searchTerm = ev.target.value.trim();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadLogs, 200);
});

loadLogs();
setInterval(loadLogs, 30000);
</script>
</body>
</html>"""


@app.route("/logs")
@_auth_required
def logs_page():
    from flask import render_template_string
    title = _config.get("web", "site_title", fallback="Terrarium Monitor") if _config else "Terrarium Monitor"
    return render_template_string(LOGS_HTML, site_title=title)


@app.route("/api/logs")
@_auth_required
def api_logs():
    """Return filtered events. Query: hours, categories=a,b,c, q (text search)."""
    hours = request.args.get("hours", 24, type=int)
    cats_param = request.args.get("categories", "")
    q = request.args.get("q", "").strip().lower()
    events = _data_logger.get_all_events(hours=hours) if _data_logger else []
    cat_set = {c.strip() for c in cats_param.split(",") if c.strip()} if cats_param else None
    if cat_set:
        events = [e for e in events if e.get("category") in cat_set]
    if q:
        events = [e for e in events
                  if q in (e.get("message") or "").lower()
                  or q in (e.get("category") or "").lower()]
    for e in events:
        e["ts_local"] = _utc_to_local(e["ts"])
    return jsonify({"events": events, "count": len(events)})


TIMELAPSE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Timelapse – {{ site_title }}</title>
<style>
  :root{--bg:#0a0a14;--card:#14142a;--accent:#00c878;--text:#e0e0f0;--grey:#7878a0}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:system-ui,sans-serif;padding:1rem}
  h1{color:var(--accent);margin-bottom:.25rem;font-size:1.3rem}
  .nav{margin-bottom:1rem}
  .nav a{color:var(--accent);text-decoration:none;margin-right:1rem;font-size:.9rem}
  #viewer{position:relative;background:#000;border-radius:12px;overflow:hidden;
          max-width:900px;margin:0 auto}
  #viewer img{width:100%;display:block;min-height:200px;object-fit:contain}
  #overlay{position:absolute;bottom:0;left:0;right:0;background:rgba(0,0,0,.6);
            padding:.5rem 1rem;display:flex;align-items:center;gap:.75rem;flex-wrap:wrap}
  #ts{color:#ccc;font-size:.8rem;flex:1;min-width:120px}
  #counter{color:var(--grey);font-size:.8rem;white-space:nowrap}
  .ctrl{background:#222;color:#fff;border:1px solid #444;border-radius:6px;
        padding:.3rem .7rem;cursor:pointer;font-size:.85rem}
  .ctrl:hover{background:#333}
  .ctrl.active{background:var(--accent);color:#000;border-color:var(--accent)}
  #scrubber{width:100%;accent-color:var(--accent);cursor:pointer}
  #speed-wrap{display:flex;align-items:center;gap:.4rem;font-size:.8rem;color:var(--grey)}
  #info{text-align:center;color:var(--grey);font-size:.85rem;margin:.5rem auto;max-width:900px}
  #loading{text-align:center;padding:3rem;color:var(--grey)}
</style>
</head>
<body>
<h1>🎬 Timelapse Viewer</h1>
<div class="nav">
  <a href="/">← Dashboard</a>
</div>

<div id="loading">Loading frames...</div>
<div id="viewer" style="display:none">
  <img id="frame" src="" alt="timelapse frame">
  <div id="overlay">
    <button class="ctrl" onclick="prevFrame()">⏮</button>
    <button class="ctrl" id="playbtn" onclick="togglePlay()">▶ Play</button>
    <button class="ctrl" onclick="nextFrame()">⏭</button>
    <div id="speed-wrap">
      Speed:
      <button class="ctrl" onclick="setFps(2)">2fps</button>
      <button class="ctrl active" onclick="setFps(5)">5fps</button>
      <button class="ctrl" onclick="setFps(10)">10fps</button>
      <button class="ctrl" onclick="setFps(24)">24fps</button>
    </div>
    <span id="counter"></span>
    <span id="ts"></span>
  </div>
</div>
<div style="max-width:900px;margin:.5rem auto">
  <input type="range" id="scrubber" min="0" value="0" oninput="scrubTo(this.value)">
</div>
<div id="info"></div>

<script>
let frames = [];
let current = 0;
let playing = false;
let fps = 5;
let timer = null;

async function loadFrames(){
  const r = await fetch('/api/timelapse/frames');
  const d = await r.json();
  frames = d.frames;
  document.getElementById('loading').style.display = 'none';
  if(!frames.length){
    document.getElementById('info').textContent = 'No timelapse frames found.';
    return;
  }
  document.getElementById('viewer').style.display = 'block';
  document.getElementById('scrubber').max = frames.length - 1;
  document.getElementById('info').textContent =
    frames.length + ' frames  |  ' + d.oldest + ' → ' + d.newest +
    '  |  ' + d.retention_days + ' day retention';
  showFrame(0);
}

function showFrame(idx){
  if(!frames.length) return;
  current = Math.max(0, Math.min(idx, frames.length - 1));
  document.getElementById('frame').src = '/api/timelapse/frame/' + frames[current].name + '?t=' + Date.now();
  document.getElementById('ts').textContent = frames[current].ts;
  document.getElementById('counter').textContent = (current+1) + ' / ' + frames.length;
  document.getElementById('scrubber').value = current;
}

function nextFrame(){
  if(current >= frames.length - 1){ stopPlay(); return; }
  showFrame(current + 1);
}
function prevFrame(){ showFrame(current - 1); }
function scrubTo(v){ showFrame(parseInt(v)); }

function togglePlay(){
  playing ? stopPlay() : startPlay();
}
function startPlay(){
  playing = true;
  document.getElementById('playbtn').textContent = '⏸ Pause';
  document.getElementById('playbtn').classList.add('active');
  tick();
}
function stopPlay(){
  playing = false;
  clearTimeout(timer);
  document.getElementById('playbtn').textContent = '▶ Play';
  document.getElementById('playbtn').classList.remove('active');
}
function tick(){
  if(!playing) return;
  if(current >= frames.length - 1){
    showFrame(0); // loop
  } else {
    nextFrame();
  }
  timer = setTimeout(tick, 1000 / fps);
}
function setFps(f){
  fps = f;
  document.querySelectorAll('#speed-wrap .ctrl').forEach(b=>{
    b.classList.toggle('active', b.textContent === f+'fps');
  });
}

loadFrames();
</script>
</body>
</html>"""


@app.route("/timelapse")
@_auth_required
def timelapse_page():
    from flask import render_template_string
    title = _config.get("web", "site_title", fallback="Terrarium Monitor") if _config else "Terrarium Monitor"
    return render_template_string(TIMELAPSE_HTML, site_title=title)


@app.route("/api/timelapse/frames")
@_auth_required
def api_timelapse_frames():
    """Return sorted list of timelapse frame filenames and timestamps."""
    if not _camera or not _camera.enabled:
        return jsonify({"frames": [], "oldest": "", "newest": "", "retention_days": 0})
    tl_path = _camera.tl_path
    if not tl_path.exists():
        return jsonify({"frames": [], "oldest": "", "newest": "", "retention_days": 0})

    files = sorted(tl_path.glob("frame_*.jpg"), key=lambda f: f.name)
    frames = []
    for f in files:
        # Parse timestamp from filename: frame_YYYYMMDD_HHMMSS.jpg
        try:
            stem = f.stem  # frame_20260527_143022
            parts = stem.split("_", 1)[1]  # 20260527_143022
            dt = datetime.strptime(parts, "%Y%m%d_%H%M%S")
            # Apply UTC offset
            offset = _config.getfloat("general", "utc_offset_hours", fallback=0) if _config else 0
            dt = dt + timedelta(hours=offset)
            ts = dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            ts = f.stem
        frames.append({"name": f.name, "ts": ts})

    oldest = frames[0]["ts"]  if frames else ""
    newest = frames[-1]["ts"] if frames else ""
    retention = _camera.tl_retention_days if _camera else 7

    return jsonify({"frames": frames, "oldest": oldest,
                    "newest": newest, "retention_days": retention})


@app.route("/api/timelapse/frame/<filename>")
@_auth_required
def api_timelapse_frame(filename):
    """Serve a single timelapse frame."""
    if not _camera:
        return "Camera not configured", 404
    # Safety check — no path traversal
    if "/" in filename or ".." in filename:
        return "Invalid filename", 400
    path = _camera.tl_path / filename
    if not path.exists():
        return "Frame not found", 404
    from flask import send_file
    resp = send_file(str(path.resolve()), mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/api/pushover/test", methods=["POST"])
@_auth_required
def api_pushover_test():
    if not _notifier:
        return jsonify({"ok": False, "message": "Notifier not initialised."}), 500
    ok, msg = _notifier.test()
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/shutdown", methods=["POST"])
@_auth_required
def api_shutdown():
    import os, signal, threading
    threading.Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# Runner
# ------------------------------------------------------------------

def run(config):
    host = config.get("web", "host", fallback="0.0.0.0")
    port = config.getint("web", "port", fallback=8080)
    logger.info("Web dashboard starting on http://%s:%d", host, port)
    app.run(host=host, port=port, debug=False, use_reloader=False)