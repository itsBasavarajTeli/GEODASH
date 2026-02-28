import os
import csv
import io
import math
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, render_template_string, Response
from dotenv import load_dotenv

load_dotenv()

TOMTOM_API_KEY = os.getenv("TOMTOM_API_KEY", "").strip()
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "").strip()

PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_DB = os.getenv("PG_DB", "geo_dashboard")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASSWORD = os.getenv("PG_PASSWORD", "")

# Optional overrides (in case TomTom endpoints differ for your plan/version)
TOMTOM_TRAFFIC_TILE_URL = os.getenv(
    "TOMTOM_TRAFFIC_TILE_URL",
    # Common TomTom traffic flow tiles endpoint
    "https://api.tomtom.com/traffic/map/4/tile/flow/relative/{z}/{x}/{y}.png?key={key}",
)

app = Flask(__name__)


# ---------------------------
# DB helpers (NO schema changes)
# ---------------------------
def db_conn():
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB, user=PG_USER, password=PG_PASSWORD
    )


def save_to_db(query_text, place, lat, lon, weather, aqi_0_500, traffic):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO geo_search_history
                (query_text, place_name, lat, lon, temperature_c, humidity_pct, wind_speed_ms, aqi, traffic_speed_kmh)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    query_text,
                    place,
                    lat,
                    lon,
                    weather.get("temperature_c"),
                    weather.get("humidity_pct"),
                    weather.get("wind_speed_ms"),
                    aqi_0_500,  # stored in SAME 'aqi' column
                    (traffic or {}).get("currentSpeed_kmh"),
                ),
            )
        conn.commit()


def fetch_recent(limit=50):
    with db_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, query_text, place_name, lat, lon,
                       temperature_c, humidity_pct, wind_speed_ms, aqi, traffic_speed_kmh,
                       created_at
                FROM geo_search_history
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return cur.fetchall()


def fetch_today_stats():
    """UI-only stats, no new DB columns."""
    with db_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                  COUNT(*)::int AS n,
                  AVG(temperature_c)::float AS avg_temp,
                  AVG(aqi)::float AS avg_aqi,
                  MAX(aqi)::int AS max_aqi,
                  AVG(traffic_speed_kmh)::float AS avg_speed
                FROM geo_search_history
                WHERE created_at >= date_trunc('day', now())
                """
            )
            return cur.fetchone() or {}


# ---------------------------
# TomTom / OpenWeather helpers
# ---------------------------
def tomtom_geocode(query: str):
    url = f"https://api.tomtom.com/search/2/geocode/{requests.utils.quote(query)}.json"
    params = {"key": TOMTOM_API_KEY, "limit": 1}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    js = r.json()
    results = js.get("results", [])
    if not results:
        return None
    pos = results[0].get("position", {})
    lat = float(pos.get("lat"))
    lon = float(pos.get("lon"))
    place = results[0].get("address", {}).get("freeformAddress", query)
    return place, lat, lon


def tomtom_geocode_any(query: str):
    url = f"https://api.tomtom.com/search/2/geocode/{requests.utils.quote(query)}.json"
    params = {"key": TOMTOM_API_KEY, "limit": 1}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    js = r.json()
    results = js.get("results", [])
    if not results:
        return None
    pos = results[0].get("position", {})
    lat = float(pos.get("lat"))
    lon = float(pos.get("lon"))
    place = results[0].get("address", {}).get("freeformAddress", query)
    return {"place": place, "lat": lat, "lon": lon}


def tomtom_reverse(lat: float, lon: float):
    url = f"https://api.tomtom.com/search/2/reverseGeocode/{lat},{lon}.json"
    params = {"key": TOMTOM_API_KEY, "limit": 1}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    js = r.json()
    add = (js.get("addresses") or [{}])[0].get("address", {}) or {}
    place = add.get("freeformAddress") or add.get("municipality") or "My location"
    return place


def tomtom_suggest(query: str, limit: int = 6):
    # Autocomplete suggestions
    url = f"https://api.tomtom.com/search/2/search/{requests.utils.quote(query)}.json"
    params = {
        "key": TOMTOM_API_KEY,
        "limit": limit,
        "typeahead": "true",
        "idxSet": "Geo",
        "countrySet": "IN",
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    js = r.json()
    out = []
    for it in js.get("results", [])[:limit]:
        pos = it.get("position", {}) or {}
        add = it.get("address", {}) or {}
        out.append(
            {
                "label": add.get("freeformAddress") or it.get("poi", {}).get("name") or query,
                "lat": pos.get("lat"),
                "lon": pos.get("lon"),
            }
        )
    return out


def openweather_weather(lat: float, lon: float):
    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {"lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY, "units": "metric"}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    js = r.json()
    main = js.get("main", {}) or {}
    wind = js.get("wind", {}) or {}
    clouds = js.get("clouds", {}) or {}
    weather0 = (js.get("weather") or [{}])[0] or {}
    rain = (js.get("rain") or {})  # may contain {"1h":..} or {"3h":..}

    return {
        "temperature_c": main.get("temp"),
        "feels_like_c": main.get("feels_like"),
        "humidity_pct": main.get("humidity"),
        "wind_speed_ms": wind.get("speed"),
        "clouds_pct": clouds.get("all"),
        "rain_1h_mm": rain.get("1h"),
        "weather_main": weather0.get("main"),      # e.g., Clouds, Rain
        "weather_desc": weather0.get("description"),
    }


# ---------------------------
# AQI 0..500 from PM2.5 + pollutants
# ---------------------------
def _aqi_from_pm25_us(pm25_ug_m3: float):
    if pm25_ug_m3 is None:
        return None
    bps = [
        (0.0, 12.0, 0, 50),
        (12.1, 35.4, 51, 100),
        (35.5, 55.4, 101, 150),
        (55.5, 150.4, 151, 200),
        (150.5, 250.4, 201, 300),
        (250.5, 350.4, 301, 400),
        (350.5, 500.4, 401, 500),
    ]
    c = float(pm25_ug_m3)
    c = max(0.0, c)
    if c > 500.4:
        return 500
    for cl, ch, il, ih in bps:
        if cl <= c <= ch:
            aqi = ((ih - il) / (ch - cl)) * (c - cl) + il
            return int(round(aqi))
    return None


def aqi_label_500(a):
    if a is None:
        return "—"
    a = int(a)
    if a <= 50:
        return "Good"
    if a <= 100:
        return "Satisfactory"
    if a <= 200:
        return "Moderate"
    if a <= 300:
        return "Poor"
    if a <= 400:
        return "Very Poor"
    return "Severe"


def aqi_health_tip(a):
    if a is None:
        return "—"
    a = int(a)
    if a <= 50:
        return "Air is good. Enjoy outdoor activities."
    if a <= 100:
        return "Acceptable. Sensitive people should monitor symptoms."
    if a <= 200:
        return "Limit long outdoor exertion if you feel discomfort."
    if a <= 300:
        return "Sensitive groups should avoid outdoor activities."
    if a <= 400:
        return "Avoid outdoor exertion. Consider wearing a mask outdoors."
    return "Severe: Stay indoors; use air purifier if available."


def openweather_aqi_details(lat: float, lon: float):
    """
    Returns:
      - aqi_0_500
      - components pollutants
      - dominant pollutant
      - label, health_tip
    """
    url = "https://api.openweathermap.org/data/2.5/air_pollution"
    params = {"lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    js = r.json()

    comp = (js.get("list", [{}])[0] or {}).get("components", {}) or {}
    # ug/m3 fields for OpenWeather:
    # co, no, no2, o3, so2, pm2_5, pm10, nh3
    pm25 = comp.get("pm2_5")
    aqi_0_500 = _aqi_from_pm25_us(pm25) if pm25 is not None else None

    dominant = None
    if comp:
        # pick max among common pollutants (excluding nh3 if you want)
        keys = ["pm2_5", "pm10", "no2", "so2", "o3", "co"]
        best = None
        for k in keys:
            v = comp.get(k)
            if v is None:
                continue
            if (best is None) or (float(v) > float(best[1])):
                best = (k, v)
        if best:
            dominant = best[0]

    return {
        "aqi_0_500": aqi_0_500,
        "label": aqi_label_500(aqi_0_500),
        "health_tip": aqi_health_tip(aqi_0_500),
        "components": comp,
        "dominant": dominant,
    }


def tomtom_traffic(lat: float, lon: float):
    url = "https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json"
    params = {"point": f"{lat},{lon}", "key": TOMTOM_API_KEY}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    js = r.json()
    flow = js.get("flowSegmentData", {}) or {}

    cur = flow.get("currentSpeed")
    free = flow.get("freeFlowSpeed")

    ratio = None
    label = "—"
    if cur is not None and free:
        try:
            ratio = float(cur) / float(free) if float(free) > 0 else None
        except Exception:
            ratio = None

    if ratio is not None:
        if ratio >= 0.85:
            label = "Smooth"
        elif ratio >= 0.60:
            label = "Moderate"
        else:
            label = "Heavy"

    return {
        "currentSpeed_kmh": cur,
        "freeFlowSpeed_kmh": free,
        "congestion_ratio": round(ratio, 2) if ratio is not None else None,
        "congestion_label": label,
    }


# ---------------------------
# Routing: multiple modes + turn instructions
# ---------------------------
def tomtom_route(o_lat, o_lon, d_lat, d_lon, mode: str):
    """
    mode:
      fastest
      shortest
      avoid_tolls
      avoid_highways
    """
    url = f"https://api.tomtom.com/routing/1/calculateRoute/{o_lat},{o_lon}:{d_lat},{d_lon}/json"
    params = {
        "key": TOMTOM_API_KEY,
        "traffic": "true",
        "travelMode": "car",
        "computeTravelTimeFor": "all",
        "instructionsType": "text",  # enables guidanceInstructions on many plans
        "language": "en-GB",
    }

    # routeType: fastest/shortest
    if mode == "shortest":
        params["routeType"] = "shortest"
    else:
        params["routeType"] = "fastest"

    # avoid options
    # TomTom accepts avoid=... (comma-separated)
    if mode == "avoid_tolls":
        params["avoid"] = "tollRoads"
    elif mode == "avoid_highways":
        params["avoid"] = "motorways"

    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    js = r.json()

    route0 = js["routes"][0]
    summary = route0.get("summary", {}) or {}
    points = route0["legs"][0]["points"]
    coords = [[p["latitude"], p["longitude"]] for p in points]

    distance_km = (summary.get("lengthInMeters", 0) or 0) / 1000.0
    time_min = (summary.get("travelTimeInSeconds", 0) or 0) / 60.0
    delay_min = (summary.get("trafficDelayInSeconds", 0) or 0) / 60.0

    # guidance instructions (if available in response)
    instr = []
    try:
        gi = route0.get("guidance", {}).get("instructions", []) or []
        for x in gi[:8]:
            msg = x.get("message")
            dist_m = x.get("routeOffsetInMeters")
            instr.append({"message": msg, "routeOffsetInMeters": dist_m})
    except Exception:
        instr = []

    return {
        "mode": mode,
        "distance_km": round(distance_km, 2),
        "travel_time_min": round(time_min, 1),
        "traffic_delay_min": round(delay_min, 1),
        "coords": coords,
        "instructions": instr,
    }


# ---------------------------
# UI
# ---------------------------
@app.route("/")
def index():
    traffic_tile = TOMTOM_TRAFFIC_TILE_URL.replace("{key}", TOMTOM_API_KEY)
    html = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Geo Dashboard (India)</title>

  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>

  <style>
    :root{
      --bg0:#050814; --bg1:#09111f;
      --card: rgba(255,255,255,.07);
      --card2: rgba(0,0,0,.30);
      --stroke: rgba(255,255,255,.13);
      --text: rgba(255,255,255,.93);
      --muted: rgba(255,255,255,.68);
      --accent1:#7c3aed; --accent2:#06b6d4;
      --radius:22px;
    }
    *{box-sizing:border-box}
    body{
      margin:0; color:var(--text);
      font-family: ui-sans-serif, system-ui, Segoe UI, Arial;
      background:
        radial-gradient(1100px 700px at 20% -20%, rgba(124,58,237,.55), transparent 60%),
        radial-gradient(900px 600px at 95% 5%, rgba(6,182,212,.45), transparent 60%),
        linear-gradient(180deg, var(--bg0), var(--bg1));
      min-height:100vh; overflow-x:hidden;
    }
    .bgGlow{
      position:fixed; inset:-40px; pointer-events:none;
      background:
        radial-gradient(600px 300px at 20% 30%, rgba(124,58,237,.14), transparent 60%),
        radial-gradient(700px 400px at 80% 20%, rgba(6,182,212,.12), transparent 60%),
        radial-gradient(500px 320px at 70% 80%, rgba(124,58,237,.10), transparent 60%);
      filter: blur(2px);
      animation: floatGlow 12s ease-in-out infinite alternate;
      opacity:.9;
    }
    @keyframes floatGlow{ from{transform:translate3d(0,0,0)} to{transform:translate3d(-18px,10px,0)} }

    .topbar{
      position:sticky; top:0; z-index:50;
      backdrop-filter: blur(14px);
      background: rgba(0,0,0,.35);
      border-bottom: 1px solid var(--stroke);
    }
    .topbar-inner{
      width: min(95vw, 1700px);
      margin: 0 auto;
      padding: 16px 22px;
      display:flex; align-items:center; gap:14px;
    }
    .brand .title{font-weight:950;font-size:20px}
    .brand .sub{font-size:12px;color:var(--muted);margin-top:3px}
    .pill{
      margin-left:auto;
      padding:10px 14px;
      border-radius:999px;
      border:1px solid var(--stroke);
      background: rgba(255,255,255,.07);
      color: var(--muted);
      font-size: 12px;
      white-space:nowrap;
      animation: pillPop .35s ease;
    }
    @keyframes pillPop{ from{transform:scale(.98);opacity:.6} to{transform:scale(1);opacity:1} }

    .wrap{
      width: min(95vw, 1700px);
      margin: 0 auto;
      padding: 18px 22px 26px;
      display:grid;
      grid-template-columns: 1.35fr 0.65fr;
      gap: 16px;
    }
    .panel{
      border:1px solid var(--stroke);
      background: var(--card);
      border-radius: var(--radius);
      box-shadow: 0 22px 55px rgba(0,0,0,.38);
      overflow:hidden;
      animation: enter .55s ease both;
    }
    @keyframes enter{ from{opacity:0; transform:translateY(10px)} to{opacity:1; transform:translateY(0)} }
    .panel-pad{ padding: 16px; }

    .searchRow{ display:flex; gap:12px; align-items:center; position:relative; }
    .input{
      flex:1;
      background: rgba(255,255,255,.07);
      border: 1px solid var(--stroke);
      color: var(--text);
      border-radius: 16px;
      padding: 14px 16px;
      outline:none;
      font-size: 14px;
    }
    .btn{
      border:none; cursor:pointer; color:white;
      font-weight: 950;
      border-radius: 16px;
      padding: 14px 16px;
      background: linear-gradient(90deg, var(--accent1), var(--accent2));
      box-shadow: 0 16px 35px rgba(0,0,0,.28);
      transition: transform .15s ease, filter .2s ease;
      white-space: nowrap;
    }
    .btn:hover{ transform: translateY(-1px); filter: brightness(1.05) }
    .btn-ghost{
      background: rgba(255,255,255,.07);
      border: 1px solid var(--stroke);
      color: var(--text);
      box-shadow:none;
      font-weight: 900;
    }

    /* Autocomplete dropdown */
    .suggestBox{
      position:absolute;
      left:0; right:140px; top:54px;
      background: rgba(0,0,0,.65);
      border: 1px solid rgba(255,255,255,.14);
      border-radius: 16px;
      backdrop-filter: blur(12px);
      overflow:hidden;
      display:none;
      z-index:70;
    }
    .sugItem{
      padding: 10px 12px;
      border-bottom: 1px solid rgba(255,255,255,.08);
      cursor:pointer;
      color: rgba(255,255,255,.92);
      font-size: 13px;
    }
    .sugItem:hover{ background: rgba(255,255,255,.06); }
    .sugSmall{ display:block; color: rgba(255,255,255,.62); font-size: 11px; margin-top:2px;}

    .toolbar{
      display:flex; gap:10px; align-items:center;
      margin-top: 12px;
      color: var(--muted);
      font-size: 12px;
      flex-wrap: wrap;
    }
    .select{
      background: rgba(255,255,255,.07);
      border: 1px solid var(--stroke);
      color: var(--text);
      border-radius: 14px;
      padding: 10px 12px;
      outline:none;
      font-weight: 900;
    }

    .kpis{
      display:grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 14px;
      margin-top: 14px;
    }
    .card{
      position:relative;
      background: var(--card2);
      border:1px solid var(--stroke);
      border-radius: 18px;
      padding: 14px 14px;
      display:flex; gap: 12px; align-items:flex-start;
      overflow:hidden;
    }
    .icon{
      width:44px;height:44px;border-radius: 14px;
      display:flex;align-items:center;justify-content:center;
      background: linear-gradient(135deg, rgba(124,58,237,.62), rgba(6,182,212,.40));
      border:1px solid rgba(255,255,255,.15);
      flex:0 0 auto;
      font-size: 20px;
    }
    .label{ color: var(--muted); font-size: 12px; font-weight: 900; }
    .value{ font-size: 30px; font-weight: 980; margin-top: 4px; }
    .meta{ color: var(--muted); font-size: 12px; margin-top: 4px; }

    /* Temp fire background */
    .tempFire::before{
      content:""; position:absolute; inset:-60px; pointer-events:none;
      background:
        radial-gradient(220px 140px at 25% 70%, rgba(255,153,0,.28), transparent 60%),
        radial-gradient(220px 160px at 55% 75%, rgba(255,60,0,.22), transparent 60%),
        radial-gradient(240px 170px at 80% 70%, rgba(255,200,0,.16), transparent 60%);
      opacity:.85; animation: fireFlicker 1.2s ease-in-out infinite;
    }
    .tempFire::after{
      content:""; position:absolute; inset:0; pointer-events:none;
      background:
        radial-gradient(6px 6px at 15% 85%, rgba(255,220,150,.85), transparent 60%),
        radial-gradient(5px 5px at 35% 90%, rgba(255,200,120,.75), transparent 60%),
        radial-gradient(4px 4px at 60% 92%, rgba(255,230,160,.70), transparent 60%),
        radial-gradient(5px 5px at 80% 88%, rgba(255,210,140,.65), transparent 60%);
      opacity:.7; animation: embersUp 2.4s linear infinite;
    }
    @keyframes fireFlicker{
      0%{ transform: translate3d(0,0,0) scale(1) }
      50%{ transform: translate3d(-10px,6px,0) scale(1.02) }
      100%{ transform: translate3d(0,0,0) scale(1) }
    }
    @keyframes embersUp{ from{ transform: translateY(0); opacity:.65 } to{ transform: translateY(-26px); opacity:.15 } }

    /* AQI wind animation */
    .aqiWind svg{
      position:absolute; right:-12px; top:-6px;
      opacity:.40;
      width:150px; height:90px;
      transform: rotate(-8deg);
      pointer-events:none;
    }
    .aqiWind path{
      stroke: rgba(6,182,212,.85);
      stroke-width: 2;
      fill: none;
      stroke-linecap: round;
      stroke-dasharray: 12 10;
      animation: windMove 2.2s linear infinite;
    }
    .aqiWind path:nth-child(2){ opacity:.55; animation-duration: 2.8s }
    .aqiWind path:nth-child(3){ opacity:.35; animation-duration: 3.3s }
    @keyframes windMove{
      from { stroke-dashoffset: 0; transform: translateX(0) }
      to   { stroke-dashoffset: -60; transform: translateX(-18px) }
    }

    /* Traffic car anim */
    .carLane{
      position:absolute; left:0; right:0; bottom:8px; height:18px;
      opacity:.18;
      background: linear-gradient(90deg, transparent, rgba(255,255,255,.25), transparent);
    }
    .car{
      position:absolute; bottom:6px; left:-30px;
      font-size: 16px;
      animation: carDrive 2.6s linear infinite;
      opacity:.65;
    }
    @keyframes carDrive{ from{transform:translateX(0)} to{transform:translateX(360px)} }

    /* AQI meter */
    .meter{
      margin-top:10px; position:relative;
      height: 14px; border-radius: 999px; overflow:hidden;
      border: 1px solid rgba(255,255,255,.14);
      background: rgba(255,255,255,.08);
    }
    .meter .seg{ height:100%; float:left; }
    .s1{ width:20%; background:#22c55e; }
    .s2{ width:20%; background:#eab308; }
    .s3{ width:20%; background:#f97316; }
    .s4{ width:20%; background:#ef4444; }
    .s5{ width:20%; background:#7f1d1d; }
    .needle{
      position:absolute; top:-6px;
      width: 2px; height: 26px;
      background: rgba(255,255,255,.95);
      transform: translateX(-1px);
    }
    .needleDot{
      position:absolute; top:-9px;
      width: 10px; height: 10px;
      border-radius: 999px;
      background: rgba(255,255,255,.95);
      transform: translateX(-5px);
    }
    .meterTicks{
      display:flex; justify-content:space-between;
      font-size:10px; color: rgba(255,255,255,.55);
      margin-top:6px; font-weight:800;
    }

    .grid2{ display:grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 14px; }
    .chartBox{ padding: 14px; height: 320px; }
    canvas{ height: 260px !important; }

    /* Map controls */
    #mapWrap{ position:relative; }
    #map{ height: 360px; border-radius: 18px; border:1px solid var(--stroke); overflow:hidden; }
    .mapCtl{
      position:absolute; right:12px; top:12px; z-index: 600;
      display:flex; align-items:center; gap:8px;
      padding:10px 10px;
      border-radius: 14px;
      background: rgba(0,0,0,.45);
      backdrop-filter: blur(10px);
      border: 1px solid rgba(255,255,255,.14);
      color: rgba(255,255,255,.9);
      font-size: 12px;
      box-shadow: 0 10px 30px rgba(0,0,0,.35);
    }
    .mapCtl select, .mapCtl button{
      background: rgba(255,255,255,.10);
      border: 1px solid rgba(255,255,255,.16);
      color: rgba(255,255,255,.92);
      border-radius: 12px;
      padding: 8px 10px;
      outline: none;
      font-weight: 900;
      cursor:pointer;
    }
    .mapCtl option{ color:#111; }

    /* Route mode pills */
    .modePills{ display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
    .pillBtn{
      padding:8px 10px; border-radius:999px;
      border:1px solid rgba(255,255,255,.14);
      background: rgba(255,255,255,.07);
      color: rgba(255,255,255,.88);
      font-weight:900; font-size:12px;
      cursor:pointer;
    }
    .pillBtn.active{
      background: linear-gradient(90deg, rgba(124,58,237,.65), rgba(6,182,212,.45));
      border-color: rgba(255,255,255,.18);
    }

    .rightHead{
      display:flex; align-items:center; justify-content:space-between;
      padding: 14px 16px;
      border-bottom: 1px solid var(--stroke);
      background: rgba(0,0,0,.18);
    }
    .feed{ max-height: 780px; overflow:auto; }
    .item{ padding: 14px 16px; border-bottom: 1px solid rgba(255,255,255,.08); }
    .item:hover{ background: rgba(255,255,255,.03); cursor:pointer; }
    .rowMini{ display:flex; gap:10px; flex-wrap:wrap; color: var(--muted); font-size: 12px; margin-top: 8px; }
    .tag{
      padding: 6px 10px;
      border-radius: 999px;
      border:1px solid rgba(255,255,255,.10);
      background: rgba(255,255,255,.05);
      font-weight: 800;
    }

    @media (max-width: 1200px){
      .wrap{ grid-template-columns: 1fr; }
      .kpis{ grid-template-columns: 1fr; }
      .grid2{ grid-template-columns: 1fr; }
      #map{ height: 320px; }
      .feed{ max-height: 420px; }
      .suggestBox{ right:0; }
    }
  </style>
</head>
<body>
  <div class="bgGlow"></div>

  <div class="topbar">
    <div class="topbar-inner">
      <div class="brand">
        <div class="title">Geo Dashboard (India)</div>
        <div class="sub">Weather + AQI + Traffic + Routing + DB history</div>
      </div>
      <div class="pill" id="placePill">No location selected</div>
    </div>
  </div>

  <div class="wrap">
    <div class="panel panel-pad">

      <!-- Search + autocomplete -->
      <div class="searchRow">
        <input class="input" id="q" placeholder="Search place (e.g., Bengaluru)" autocomplete="off"/>
        <button class="btn" onclick="doSearch()">Search</button>
        <button class="btn btn-ghost" onclick="recenter()">Recenter</button>
        <div class="suggestBox" id="sugs"></div>
      </div>

      <!-- Quick actions -->
      <div class="toolbar" style="margin-top:10px;">
        <button class="btn btn-ghost" onclick="useMyLocation()">📍 Use my location</button>
        <button class="btn btn-ghost" onclick="addFav()">⭐ Add favourite</button>
        <select class="select" id="favSel" onchange="goFav()">
          <option value="">Favourites</option>
        </select>
        <span style="margin-left:auto;font-weight:900" id="status">Ready</span>
      </div>

      <!-- Route inputs -->
      <div class="toolbar" style="margin-top:10px;">
        <input class="input" id="origin" placeholder="Origin (e.g., Bengaluru)" style="max-width:420px"/>
        <input class="input" id="destination" placeholder="Destination (e.g., Mysuru)" style="max-width:420px"/>
        <button class="btn" onclick="getRoute()">Best Route</button>
        <button class="btn btn-ghost" onclick="stopRouteAnim()">Clear</button>
        <span style="margin-left:auto; font-weight:900" id="routeInfo">—</span>
      </div>

      <!-- Route modes -->
      <div class="modePills">
        <button class="pillBtn active" id="m_fastest" onclick="setMode('fastest')">Fastest</button>
        <button class="pillBtn" id="m_shortest" onclick="setMode('shortest')">Shortest</button>
        <button class="pillBtn" id="m_avoid_tolls" onclick="setMode('avoid_tolls')">Avoid tolls</button>
        <button class="pillBtn" id="m_avoid_highways" onclick="setMode('avoid_highways')">Avoid highways</button>
      </div>

      <!-- Summary stats -->
      <div class="toolbar" style="margin-top:10px;">
        <span class="tag" id="st_n">Today: —</span>
        <span class="tag" id="st_aqi">Avg AQI: —</span>
        <span class="tag" id="st_max">Max AQI: —</span>
        <span class="tag" id="st_spd">Avg speed: —</span>
        <span class="tag" id="st_tmp">Avg temp: —</span>
        <button class="btn btn-ghost" style="margin-left:auto" onclick="exportCSV()">⬇ Export CSV</button>
      </div>

      <!-- KPI cards -->
      <div class="kpis">
        <div class="card tempFire">
          <div class="icon">🌡️</div>
          <div style="width:100%">
            <div class="label">Temperature</div>
            <div class="value" id="kTemp">—</div>
            <div class="meta" id="kHum">Humidity: —</div>
            <div class="meta" id="kWx">—</div>
          </div>
        </div>

        <div class="card aqiWind">
          <div class="icon">🫁</div>
          <div style="width:100%">
            <div class="label">AQI (0–500)</div>
            <div class="value" id="kAqi">—</div>
            <div class="meta" id="kAqiLbl">—</div>

            <div class="meter">
              <div class="seg s1"></div><div class="seg s2"></div><div class="seg s3"></div><div class="seg s4"></div><div class="seg s5"></div>
              <div class="needle" id="aqiNeedle" style="left:0%"></div>
              <div class="needleDot" id="aqiNeedleDot" style="left:0%"></div>
            </div>
            <div class="meterTicks">
              <span>0</span><span>100</span><span>200</span><span>300</span><span>400</span><span>500</span>
            </div>

            <div class="meta" id="kPoll">Pollutants: —</div>
            <div class="meta" id="kTip">Tip: —</div>
          </div>

          <svg viewBox="0 0 200 100" aria-hidden="true">
            <path d="M10 35 C40 20, 70 20, 100 35 S160 50, 190 35" />
            <path d="M20 55 C55 40, 85 40, 120 55 S170 70, 195 55" />
            <path d="M5 75 C45 62, 80 62, 115 75 S165 88, 198 75" />
          </svg>
        </div>

        <div class="card">
          <div class="icon">🚗</div>
          <div style="width:100%">
            <div class="label">Traffic</div>
            <div class="value" id="kTrf">—</div>
            <div class="meta" id="kTrf2">—</div>
            <div class="meta" id="kWind">Wind: —</div>
          </div>
          <div class="carLane"></div>
          <div class="car">🚘</div>
        </div>
      </div>

      <!-- Charts -->
      <div class="grid2">
        <div class="panel chartBox">
          <div class="label" style="margin-bottom:8px">AQI trend (latest 20)</div>
          <canvas id="cAqi"></canvas>
        </div>
        <div class="panel chartBox">
          <div class="label" style="margin-bottom:8px">Traffic speed trend (latest 20)</div>
          <canvas id="cTrf"></canvas>
        </div>
      </div>

      <!-- Map -->
      <div style="margin-top:14px" class="panel panel-pad">
        <div class="label" style="margin-bottom:10px">Map</div>
        <div id="mapWrap">
          <div class="mapCtl">
            <select id="bm" onchange="switchBasemap()">
              <option value="osm" selected>OpenStreetMap</option>
              <option value="dark">Carto Dark</option>
              <option value="sat">Esri Satellite</option>
            </select>
            <button onclick="toggleTraffic()">Traffic</button>
          </div>
          <div id="map"></div>
        </div>

        <div class="toolbar" id="turns" style="margin-top:10px; display:none;"></div>
      </div>
    </div>

    <!-- RIGHT: recent -->
    <div class="panel">
      <div class="rightHead">
        <div>
          <div style="font-weight:980; font-size:14px">Recent searches</div>
          <div style="font-size:12px;color:rgba(255,255,255,.65);margin-top:2px">Stored in PostgreSQL</div>
        </div>
        <button class="btn btn-ghost" onclick="loadRecent()">Refresh</button>
      </div>
      <div class="feed" id="list"></div>
    </div>
  </div>

<script>
  const TRAFFIC_TILE_URL = "{{TRAFFIC_TILE_URL}}";

  function clamp(n,a,b){ return Math.max(a, Math.min(b, n)); }

  let lastQuery = "";
  let lastLatLng = null;
  let autoTimer = null;
  let routeMode = "fastest";

  function setStatus(msg){ document.getElementById("status").innerText = msg; }

  function setMode(m){
    routeMode = m;
    ["fastest","shortest","avoid_tolls","avoid_highways"].forEach(x=>{
      document.getElementById("m_"+x).classList.toggle("active", x===m);
    });
  }

  // ---------------------------
  // MAP + BASEMAPS + TRAFFIC OVERLAY
  // ---------------------------
  const map = L.map('map', { zoomControl: true }).setView([20.5937, 78.9629], 5);

  const bmOSM = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19 });
  const bmDark = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    maxZoom: 20, subdomains: 'abcd'
  });
  const bmSat = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', { maxZoom: 19 });

  let currentBasemap = bmOSM;
  currentBasemap.addTo(map);

  function switchBasemap(){
    const v = document.getElementById("bm").value;
    if(currentBasemap) map.removeLayer(currentBasemap);
    currentBasemap = (v==="dark") ? bmDark : (v==="sat" ? bmSat : bmOSM);
    currentBasemap.addTo(map);
    if(trafficLayerOn && trafficLayer) trafficLayer.bringToFront();
  }

  // Traffic overlay (may require TomTom plan)
  let trafficLayerOn = false;
  let trafficLayer = null;

  function toggleTraffic(){
    if(!TRAFFIC_TILE_URL || TRAFFIC_TILE_URL.includes("key=")===false){
      alert("Traffic tiles URL not configured.");
      return;
    }
    trafficLayerOn = !trafficLayerOn;
    if(trafficLayerOn){
      if(!trafficLayer){
        trafficLayer = L.tileLayer(TRAFFIC_TILE_URL, { opacity: 0.75, maxZoom: 19 });
        trafficLayer.on('tileerror', ()=>{ setStatus("Traffic tiles not allowed for this key/plan"); });
      }
      trafficLayer.addTo(map);
      setStatus("Traffic overlay ON");
    }else{
      if(trafficLayer) map.removeLayer(trafficLayer);
      setStatus("Traffic overlay OFF");
    }
  }

  // Scale bar
  L.control.scale({ imperial:false }).addTo(map);

  // Markers
  let marker = null;
  function setMarker(lat, lon, place){
    lastLatLng = [lat, lon];
    if(marker) marker.remove();
    marker = L.marker([lat, lon]).addTo(map).bindPopup(place).openPopup();
  }
  function recenter(){
    if(!lastLatLng) return;
    map.setView(lastLatLng, 12, { animate: true });
  }

  // Google-like label icon
  function labelIcon(text){
    const safe = (text||"").replace(/</g,"&lt;").replace(/>/g,"&gt;");
    return L.divIcon({
      className:"",
      html: `<div style="
        padding:6px 10px; border-radius:999px;
        background: rgba(0,0,0,.62);
        border:1px solid rgba(255,255,255,.18);
        color: rgba(255,255,255,.92);
        font-weight: 950; font-size: 12px;
        backdrop-filter: blur(10px);
        box-shadow: 0 10px 22px rgba(0,0,0,.35);
        white-space:nowrap;
      ">${safe}</div>`,
      iconSize: [1,1],
      iconAnchor: [0,0]
    });
  }

  // ---------------------------
  // CHARTS
  // ---------------------------
  function makeGradient(ctx){
    const g = ctx.createLinearGradient(0, 0, 0, 260);
    g.addColorStop(0, "rgba(6,182,212,.35)");
    g.addColorStop(1, "rgba(6,182,212,0)");
    return g;
  }

  const ctxA = document.getElementById("cAqi").getContext("2d");
  const ctxT = document.getElementById("cTrf").getContext("2d");

  const chartAqi = new Chart(ctxA, {
    type:"line",
    data:{ labels:[], datasets:[{ data:[], tension:.35, borderWidth:2.5, pointRadius:3.5, fill:true, backgroundColor: makeGradient(ctxA) }]},
    options:{ responsive:true, maintainAspectRatio:false, plugins:{ legend:{ display:false } }, animation:{ duration:900 }, scales:{} }
  });
  const chartTrf = new Chart(ctxT, {
    type:"line",
    data:{ labels:[], datasets:[{ data:[], tension:.35, borderWidth:2.5, pointRadius:3.5, fill:true, backgroundColor: makeGradient(ctxT) }]},
    options:{ responsive:true, maintainAspectRatio:false, plugins:{ legend:{ display:false } }, animation:{ duration:900 }, scales:{} }
  });

  function setAqiNeedle(aqi){
    const v = (aqi==null) ? 0 : clamp(Number(aqi), 0, 500);
    const pct = (v / 500) * 100;
    document.getElementById("aqiNeedle").style.left = pct + "%";
    document.getElementById("aqiNeedleDot").style.left = pct + "%";
  }

  // ---------------------------
  // AUTOCOMPLETE
  // ---------------------------
  let sugTimer = null;
  const sugBox = document.getElementById("sugs");
  const qEl = document.getElementById("q");

  qEl.addEventListener("input", ()=>{
    const q = qEl.value.trim();
    if(sugTimer) clearTimeout(sugTimer);
    if(q.length < 3){ sugBox.style.display="none"; return; }
    sugTimer = setTimeout(()=>loadSuggest(q), 180);
  });

  qEl.addEventListener("blur", ()=> setTimeout(()=>{ sugBox.style.display="none"; }, 150));

  async function loadSuggest(q){
    try{
      const r = await fetch("/api/suggest?q="+encodeURIComponent(q));
      const js = await r.json();
      if(js.error){ return; }
      if(!js.items || js.items.length===0){ sugBox.style.display="none"; return; }
      sugBox.innerHTML = "";
      js.items.forEach(it=>{
        const d = document.createElement("div");
        d.className = "sugItem";
        d.innerHTML = `${it.label}<span class="sugSmall">${Number(it.lat).toFixed(5)}, ${Number(it.lon).toFixed(5)}</span>`;
        d.onclick = ()=>{
          qEl.value = it.label;
          sugBox.style.display="none";
          doSearch();
        };
        sugBox.appendChild(d);
      });
      sugBox.style.display = "block";
    }catch(e){}
  }

  // ---------------------------
  // FAVOURITES (localStorage)
  // ---------------------------
  function loadFavs(){
    const raw = localStorage.getItem("geo_favs") || "[]";
    let favs = [];
    try{ favs = JSON.parse(raw) }catch(e){ favs=[] }
    const sel = document.getElementById("favSel");
    sel.innerHTML = `<option value="">Favourites</option>`;
    favs.forEach((f, i)=>{
      const opt = document.createElement("option");
      opt.value = String(i);
      opt.innerText = f.name;
      sel.appendChild(opt);
    });
  }
  function addFav(){
    const q = (document.getElementById("q").value || "").trim();
    if(!q){ alert("Search a place first"); return; }
    const raw = localStorage.getItem("geo_favs") || "[]";
    let favs = [];
    try{ favs = JSON.parse(raw) }catch(e){ favs=[] }
    if(favs.find(x=>x.name===q)){ alert("Already in favourites"); return; }
    favs.push({name:q});
    localStorage.setItem("geo_favs", JSON.stringify(favs));
    loadFavs();
    setStatus("Added favourite ✓");
  }
  function goFav(){
    const idx = document.getElementById("favSel").value;
    if(idx===""){ return; }
    const raw = localStorage.getItem("geo_favs") || "[]";
    let favs = [];
    try{ favs = JSON.parse(raw) }catch(e){ favs=[] }
    const f = favs[Number(idx)];
    if(!f) return;
    document.getElementById("q").value = f.name;
    doSearch();
  }

  // ---------------------------
  // MY LOCATION
  // ---------------------------
  async function useMyLocation(){
    if(!navigator.geolocation){ alert("Geolocation not supported"); return; }
    setStatus("Getting location…");
    navigator.geolocation.getCurrentPosition(async (pos)=>{
      const lat = pos.coords.latitude;
      const lon = pos.coords.longitude;
      try{
        const r = await fetch("/api/reverse?lat="+lat+"&lon="+lon);
        const js = await r.json();
        const name = js.place || "My location";
        document.getElementById("q").value = name;
        await doSearch();
      }catch(e){
        setStatus("Location error");
      }
    }, ()=>{ setStatus("Location permission denied"); }, { enableHighAccuracy:true, timeout:8000 });
  }

  // ---------------------------
  // RECENT + STATS + EXPORT
  // ---------------------------
  async function loadStats(){
    const r = await fetch("/api/stats");
    const js = await r.json();
    document.getElementById("st_n").innerText = "Today: " + (js.n ?? "—");
    document.getElementById("st_aqi").innerText = "Avg AQI: " + (js.avg_aqi!=null ? js.avg_aqi.toFixed(0) : "—");
    document.getElementById("st_max").innerText = "Max AQI: " + (js.max_aqi ?? "—");
    document.getElementById("st_spd").innerText = "Avg speed: " + (js.avg_speed!=null ? js.avg_speed.toFixed(0)+" km/h" : "—");
    document.getElementById("st_tmp").innerText = "Avg temp: " + (js.avg_temp!=null ? js.avg_temp.toFixed(1)+" °C" : "—");
  }

  function exportCSV(){
    window.open("/api/export?limit=200", "_blank");
  }

  async function loadRecent(){
    const r = await fetch("/api/recent");
    const js = await r.json();
    const el = document.getElementById("list");
    el.innerHTML = "";

    js.rows.forEach(row=>{
      const d = document.createElement("div");
      d.className="item";
      const place = row.place_name || row.query_text;
      d.innerHTML = `
        <div style="font-weight:950">${place}</div>
        <div class="rowMini">
          <span class="tag">${row.created_at.slice(0,19).replace("T"," ")}</span>
          <span class="tag">Temp: ${row.temperature_c ?? "—"} °C</span>
          <span class="tag">AQI: ${row.aqi ?? "—"} / 500</span>
          <span class="tag">Speed: ${row.traffic_speed_kmh ?? "—"} km/h</span>
        </div>
      `;
      d.onclick = ()=>{
        document.getElementById("q").value = row.query_text || place;
        if(row.lat && row.lon){
          setMarker(row.lat, row.lon, place);
          document.getElementById("placePill").innerText = `${place} (${Number(row.lat).toFixed(5)}, ${Number(row.lon).toFixed(5)})`;
          recenter();
        }
      };
      el.appendChild(d);
    });

    const last = js.rows.slice(0,20).reverse();
    chartAqi.data.labels = last.map(x=>x.created_at.slice(11,16));
    chartAqi.data.datasets[0].data = last.map(x=>x.aqi);
    chartAqi.update();

    chartTrf.data.labels = last.map(x=>x.created_at.slice(11,16));
    chartTrf.data.datasets[0].data = last.map(x=>x.traffic_speed_kmh);
    chartTrf.update();
  }

  // ---------------------------
  // SEARCH
  // ---------------------------
  async function doSearch(){
    const q = document.getElementById("q").value.trim();
    if(!q) return;
    lastQuery = q;
    setStatus("Fetching…");

    const r = await fetch("/api/search?query=" + encodeURIComponent(q));
    const js = await r.json();
    if(js.error){
      alert(js.error);
      setStatus("Error");
      return;
    }

    document.getElementById("placePill").innerText =
      `${js.place} (${js.lat.toFixed(5)}, ${js.lon.toFixed(5)})`;

    // Weather
    document.getElementById("kTemp").innerText = (js.temperature_c ?? "—") + (js.temperature_c!=null ? " °C" : "");
    document.getElementById("kHum").innerText = "Humidity: " + (js.humidity_pct ?? "—") + (js.humidity_pct!=null ? " %" : "");
    document.getElementById("kWind").innerText = "Wind: " + (js.wind_speed_ms ?? "—") + (js.wind_speed_ms!=null ? " m/s" : "");

    const wxBits = [];
    if(js.feels_like_c!=null) wxBits.push("Feels like " + js.feels_like_c + " °C");
    if(js.clouds_pct!=null) wxBits.push("Clouds " + js.clouds_pct + "%");
    if(js.rain_1h_mm!=null) wxBits.push("Rain(1h) " + js.rain_1h_mm + " mm");
    if(js.weather_desc) wxBits.push(js.weather_desc);
    document.getElementById("kWx").innerText = wxBits.length ? wxBits.join(" • ") : "—";

    // AQI details
    document.getElementById("kAqi").innerText = (js.aqi?.aqi_0_500 ?? "—");
    document.getElementById("kAqiLbl").innerText = js.aqi?.label ?? "—";
    setAqiNeedle(js.aqi?.aqi_0_500);

    const comp = js.aqi?.components || {};
    const dom = js.aqi?.dominant;
    const fmt = (k)=> (comp[k]!=null ? `${k.toUpperCase()}:${comp[k]}` : null);
    const pieces = ["pm2_5","pm10","no2","so2","o3","co"].map(fmt).filter(Boolean);
    document.getElementById("kPoll").innerText =
      (pieces.length ? ("Pollutants: " + pieces.join(" • ")) : "Pollutants: —") +
      (dom ? (" • Dominant: " + dom.toUpperCase()) : "");
    document.getElementById("kTip").innerText = "Tip: " + (js.aqi?.health_tip ?? "—");

    // Traffic
    const sp = js.traffic?.currentSpeed_kmh;
    const ff = js.traffic?.freeFlowSpeed_kmh;
    const lbl = js.traffic?.congestion_label;
    document.getElementById("kTrf").innerText = (sp ?? "—") + (sp!=null ? " km/h" : "");
    document.getElementById("kTrf2").innerText =
      (lbl ? (lbl + " • ") : "") + "Free flow: " + (ff ?? "—") + (ff!=null ? " km/h" : "");

    // Map marker
    setMarker(js.lat, js.lon, js.place);
    map.setView([js.lat, js.lon], 12, { animate:true });

    await loadRecent();
    await loadStats();
    setStatus("Updated ✓");
  }

  // ---------------------------
  // ROUTING: multiple modes + labels + instructions
  // ---------------------------
  let routeLine = null;
  let carMarker = null;
  let carTimer = null;
  let originMarker = null;
  let destMarker = null;

  function stopRouteAnim(){
    if(carTimer){ clearInterval(carTimer); carTimer = null; }
    if(carMarker){ map.removeLayer(carMarker); carMarker = null; }
    if(routeLine){ map.removeLayer(routeLine); routeLine = null; }
    if(originMarker){ map.removeLayer(originMarker); originMarker = null; }
    if(destMarker){ map.removeLayer(destMarker); destMarker = null; }
    document.getElementById("turns").style.display = "none";
    document.getElementById("turns").innerHTML = "";
    document.getElementById("routeInfo").innerText = "—";
  }

  function fmtTime(mins){
    if(mins==null) return "—";
    const m = Math.max(0, Number(mins));
    if(m < 60) return `${m.toFixed(0)} min`;
    const h = Math.floor(m/60);
    const r = Math.round(m - h*60);
    return `${h} hr ${r} min`;
  }

  async function getRoute(){
    const o = document.getElementById("origin").value.trim();
    const d = document.getElementById("destination").value.trim();
    if(!o || !d){ alert("Enter origin and destination"); return; }

    document.getElementById("routeInfo").innerText = "Fetching route…";

    const r = await fetch("/api/route?origin=" + encodeURIComponent(o) + "&destination=" + encodeURIComponent(d) + "&mode=" + encodeURIComponent(routeMode));
    const js = await r.json();
    if(js.error){
      alert(js.error);
      document.getElementById("routeInfo").innerText = "—";
      return;
    }

    stopRouteAnim();

    const coords = js.route.coords;
    document.getElementById("routeInfo").innerText =
      `(${js.route.mode}) Distance ${js.route.distance_km} km • ETA ${fmtTime(js.route.travel_time_min)} • Delay ${fmtTime(js.route.traffic_delay_min)}`;

    // Route line
    routeLine = L.polyline(coords, { weight: 6, opacity: 0.9 }).addTo(map);
    map.fitBounds(routeLine.getBounds(), { padding:[20,20] });

    // Origin/Destination labels (pill)
    originMarker = L.marker([js.origin.lat, js.origin.lon], { icon: labelIcon("Origin: "+(js.origin.place || o)) }).addTo(map);
    destMarker = L.marker([js.destination.lat, js.destination.lon], { icon: labelIcon("Destination: "+(js.destination.place || d)) }).addTo(map);

    // Car animation along route
    carMarker = L.marker(coords[0], {
      icon: L.divIcon({ className:"", html:`<div style="font-size:22px;filter:drop-shadow(0 8px 12px rgba(0,0,0,.35));">🚗</div>` })
    }).addTo(map);

    let i = 0;
    const stepMs = 55;
    carTimer = setInterval(()=>{
      i++;
      if(i >= coords.length){
        clearInterval(carTimer); carTimer = null; return;
      }
      carMarker.setLatLng(coords[i]);
    }, stepMs);

    // Turn-by-turn (if provided)
    const turns = js.route.instructions || [];
    if(turns.length){
      const box = document.getElementById("turns");
      box.style.display = "flex";
      box.innerHTML = "";
      turns.forEach(t=>{
        const s = document.createElement("span");
        s.className = "tag";
        s.innerText = t.message || "—";
        box.appendChild(s);
      });
    }else{
      document.getElementById("turns").style.display = "none";
    }
  }

  // ---------------------------
  // INIT
  // ---------------------------
  async function init(){
    loadFavs();
    await loadRecent();
    await loadStats();
  }
  init();
</script>
</body>
</html>
"""
    return render_template_string(html, TRAFFIC_TILE_URL=traffic_tile)


# ---------------------------
# API endpoints
# ---------------------------
@app.route("/api/search")
def api_search():
    if not TOMTOM_API_KEY or not OPENWEATHER_API_KEY:
        return jsonify({"error": "Missing API keys. Please set TOMTOM_API_KEY and OPENWEATHER_API_KEY in .env"}), 400

    query = (request.args.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400

    geo = tomtom_geocode(query)
    if not geo:
        return jsonify({"error": "Location not found"}), 404

    place, lat, lon = geo
    weather = openweather_weather(lat, lon)
    aqi = openweather_aqi_details(lat, lon)
    traffic = tomtom_traffic(lat, lon)

    save_to_db(query, place, lat, lon, weather, aqi.get("aqi_0_500"), traffic)

    return jsonify(
        {
            "query": query,
            "place": place,
            "lat": lat,
            "lon": lon,
            **weather,
            "aqi": aqi,
            "traffic": traffic,
        }
    )


@app.route("/api/recent")
def api_recent():
    rows = fetch_recent(limit=50)
    for r in rows:
        r["created_at"] = r["created_at"].isoformat()
    return jsonify({"rows": rows})


@app.route("/api/stats")
def api_stats():
    return jsonify(fetch_today_stats())


@app.route("/api/export")
def api_export():
    limit = int(request.args.get("limit") or 200)
    rows = fetch_recent(limit=limit)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "created_at",
            "query_text",
            "place_name",
            "lat",
            "lon",
            "temperature_c",
            "humidity_pct",
            "wind_speed_ms",
            "aqi_0_500",
            "traffic_speed_kmh",
        ]
    )
    for r in rows:
        w.writerow(
            [
                r["created_at"].isoformat(),
                r.get("query_text"),
                r.get("place_name"),
                r.get("lat"),
                r.get("lon"),
                r.get("temperature_c"),
                r.get("humidity_pct"),
                r.get("wind_speed_ms"),
                r.get("aqi"),
                r.get("traffic_speed_kmh"),
            ]
        )

    out = buf.getvalue()
    return Response(
        out,
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="geo_history.csv"'},
    )


@app.route("/api/suggest")
def api_suggest():
    if not TOMTOM_API_KEY:
        return jsonify({"error": "Missing TOMTOM_API_KEY in .env"}), 400
    q = (request.args.get("q") or "").strip()
    if len(q) < 3:
        return jsonify({"items": []})
    try:
        return jsonify({"items": tomtom_suggest(q, limit=6)})
    except Exception as e:
        return jsonify({"items": [], "error": str(e)}), 500


@app.route("/api/reverse")
def api_reverse():
    if not TOMTOM_API_KEY:
        return jsonify({"error": "Missing TOMTOM_API_KEY in .env"}), 400
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
    except Exception:
        return jsonify({"error": "lat/lon required"}), 400
    try:
        place = tomtom_reverse(lat, lon)
        return jsonify({"place": place})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/route")
def api_route():
    if not TOMTOM_API_KEY:
        return jsonify({"error": "Missing TOMTOM_API_KEY in .env"}), 400

    origin = (request.args.get("origin") or "").strip()
    destination = (request.args.get("destination") or "").strip()
    mode = (request.args.get("mode") or "fastest").strip()

    if not origin or not destination:
        return jsonify({"error": "origin and destination are required"}), 400

    o = tomtom_geocode_any(origin)
    d = tomtom_geocode_any(destination)
    if not o:
        return jsonify({"error": f"Origin not found: {origin}"}), 404
    if not d:
        return jsonify({"error": f"Destination not found: {destination}"}), 404

    try:
        route = tomtom_route(o["lat"], o["lon"], d["lat"], d["lon"], mode=mode)
    except requests.HTTPError as e:
        # if avoid/instructions not allowed by plan, still show readable message
        return jsonify({"error": f"Routing API error. Check TomTom key/plan. Details: {str(e)}"}), 502

    return jsonify({"origin": o, "destination": d, "route": route})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
