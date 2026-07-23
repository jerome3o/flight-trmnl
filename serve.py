#!/usr/bin/env python3
"""
serve.py — local server for window-flights.html + machine-readable API

Endpoints:
  /                 the webapp
  /proxy?...        raw upstream passthrough (used by the webapp)
  /api/visible      JSON array of aircraft currently in the view cone

/api/visible with no parameters uses the built-in window defaults
(168° SSE, 145° FOV, 5 km, min elevation 8° from the configured location). Every
parameter can be overridden:

  /api/visible?lat=51.5074&lon=-0.1278&bearing=168&fov=145&range_km=5&min_elev=8&source=adsblol

Response: a JSON array, one object per aircraft, nearest first:

  [{"hex":"406b8b","callsign":"BAW172","type":"B772",
    "lat":51.502,"lon":-0.041,"dist_km":2.3,"bearing_deg":141.2,
    "elevation_deg":24.7,"alt_ft":6250,"gs_kt":231,"track_deg":268.5}, ...]

alt_ft / gs_kt / track_deg are null when the aircraft isn't broadcasting
them. The endpoint sends Access-Control-Allow-Origin: * so any web page
can consume it. Run with --host 0.0.0.0 to share it on your network.

Usage:
    python serve.py                      # http://localhost:8000
    python serve.py --host 0.0.0.0       # reachable from other machines
    python serve.py --port 9000
"""

import argparse
import os
import json
import math
import re
import sys
import hmac
import hashlib
import urllib.request
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

EARTH_R_KM = 6371.0088

# Observer location + view cone. LAT and LON are REQUIRED and have no built-in
# value on purpose — this repo ships no real coordinates. Set them (and any of
# the optional overrides) via env vars or a local .env (see .env.example).
DEFAULTS = {
    "lat": None,
    "lon": None,
    "bearing": 168.0,
    "fov": 145.0,
    "range_km": 5.0,
    "min_elev": 8.0,
    "source": "adsblol",
}

# Environment overrides so the docker image is configurable without code edits
for _key, _env in [("lat", "LAT"), ("lon", "LON"), ("bearing", "BEARING"),
                   ("fov", "FOV"), ("range_km", "RANGE_KM"), ("min_elev", "MIN_ELEV")]:
    if os.environ.get(_env):
        DEFAULTS[_key] = float(os.environ[_env])

if DEFAULTS["lat"] is None or DEFAULTS["lon"] is None:
    raise SystemExit(
        "LAT and LON must be set — this build ships no default location.\n"
        "Copy .env.example to .env and set your observer coordinates, or pass\n"
        "LAT/LON as environment variables to the container."
    )
if os.environ.get("SOURCE"):
    DEFAULTS["source"] = os.environ["SOURCE"]

UPSTREAMS = {
    "adsblol":       "https://api.adsb.lol/v2/point/{lat}/{lon}/{nm}",
    "airplaneslive": "https://api.airplanes.live/v2/point/{lat}/{lon}/{nm}",
    "adsbfi":        "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{nm}",
}

# Fallback names for common ICAO type designators, used when the upstream
# doesn't supply its own "desc" field. Not exhaustive — unknown types just
# return type_desc: null alongside the raw code.
TYPE_NAMES = {
    "A319": "Airbus A319", "A320": "Airbus A320", "A321": "Airbus A321",
    "A20N": "Airbus A320neo", "A21N": "Airbus A321neo",
    "A332": "Airbus A330-200", "A333": "Airbus A330-300", "A339": "Airbus A330-900neo",
    "A359": "Airbus A350-900", "A35K": "Airbus A350-1000", "A388": "Airbus A380-800",
    "B734": "Boeing 737-400", "B737": "Boeing 737-700", "B738": "Boeing 737-800",
    "B38M": "Boeing 737 MAX 8", "B39M": "Boeing 737 MAX 9",
    "B744": "Boeing 747-400", "B748": "Boeing 747-8",
    "B752": "Boeing 757-200", "B763": "Boeing 767-300",
    "B772": "Boeing 777-200", "B77W": "Boeing 777-300ER", "B779": "Boeing 777-9",
    "B788": "Boeing 787-8", "B789": "Boeing 787-9", "B78X": "Boeing 787-10",
    "BCS1": "Airbus A220-100", "BCS3": "Airbus A220-300",
    "E190": "Embraer E190", "E195": "Embraer E195", "E290": "Embraer E190-E2",
    "E295": "Embraer E195-E2", "E75L": "Embraer E175",
    "AT75": "ATR 72-500", "AT76": "ATR 72-600", "DH8D": "Dash 8 Q400",
    "C25B": "Cessna Citation CJ3", "C56X": "Cessna Citation XLS",
    "C680": "Cessna Citation Sovereign", "GLF6": "Gulfstream G650",
    "GL7T": "Bombardier Global 7500", "F2TH": "Dassault Falcon 2000",
    "PC12": "Pilatus PC-12", "SR22": "Cirrus SR22",
    "EC35": "Airbus H135 helicopter", "EC45": "Airbus H145 helicopter",
    "A139": "AgustaWestland AW139 helicopter", "A169": "AgustaWestland AW169 helicopter",
    "S76": "Sikorsky S-76 helicopter", "R44": "Robinson R44 helicopter",
}


# Common ICAO airline callsign prefixes seen over London. Callsign-derived
# airline reflects the operating carrier; ownOp from the registration DB is
# the fallback (useful for bizjets, helicopters, GA).
AIRLINES = {
    "BAW": "British Airways", "SHT": "British Airways Shuttle", "CFE": "BA CityFlyer",
    "EZY": "easyJet", "EJU": "easyJet Europe", "EZS": "easyJet Switzerland",
    "RYR": "Ryanair", "RUK": "Ryanair UK", "WZZ": "Wizz Air", "WUK": "Wizz Air UK",
    "VIR": "Virgin Atlantic", "TOM": "TUI Airways", "EXS": "Jet2",
    "LOG": "Loganair", "EIN": "Aer Lingus", "EAI": "Aer Lingus UK",
    "KLM": "KLM", "AFR": "Air France", "DLH": "Lufthansa", "SWR": "Swiss",
    "AUA": "Austrian", "BEL": "Brussels Airlines", "SAS": "SAS", "FIN": "Finnair",
    "IBE": "Iberia", "VLG": "Vueling", "TAP": "TAP Air Portugal", "LOT": "LOT Polish",
    "THY": "Turkish Airlines", "ELY": "El Al", "MSR": "EgyptAir",
    "UAE": "Emirates", "QTR": "Qatar Airways", "ETD": "Etihad", "GFA": "Gulf Air",
    "SVA": "Saudia", "KAC": "Kuwait Airways", "RJA": "Royal Jordanian",
    "AAL": "American Airlines", "UAL": "United", "DAL": "Delta", "ACA": "Air Canada",
    "SIA": "Singapore Airlines", "CPA": "Cathay Pacific", "ANA": "All Nippon",
    "JAL": "Japan Airlines", "KAL": "Korean Air", "AIC": "Air India",
    "CCA": "Air China", "CES": "China Eastern", "CSN": "China Southern",
    "ITY": "ITA Airways", "NSZ": "Norse Atlantic", "NOZ": "Norwegian",
    "PGT": "Pegasus", "AEE": "Aegean",
}


# Country of registry from the registration prefix (the aircraft's "flag").
# Longest prefix wins; not exhaustive, unknowns return None.
REG_PREFIXES = {
    "G-": "GB", "M-": "IM", "2-": "GG", "EI-": "IE", "N": "US", "C-": "CA",
    "D-": "DE", "F-": "FR", "PH-": "NL", "OO-": "BE", "LX-": "LU", "HB-": "CH",
    "OE-": "AT", "EC-": "ES", "CS-": "PT", "I-": "IT", "SE-": "SE", "LN-": "NO",
    "OY-": "DK", "OH-": "FI", "SP-": "PL", "OK-": "CZ", "OM-": "SK", "HA-": "HU",
    "9A-": "HR", "S5-": "SI", "LZ-": "BG", "YR-": "RO", "SX-": "GR", "TC-": "TR",
    "9H-": "MT", "TF-": "IS", "YL-": "LV", "LY-": "LT", "ES-": "EE", "EW-": "BY",
    "UR-": "UA", "RA-": "RU", "4L-": "GE", "EK-": "AM", "UP-": "KZ",
    "4X-": "IL", "SU-": "EG", "JY-": "JO", "OD-": "LB", "HZ-": "SA", "A6-": "AE",
    "A7-": "QA", "A9C-": "BH", "9K-": "KW", "EP-": "IR", "AP-": "PK", "VT-": "IN",
    "4R-": "LK", "9V-": "SG", "9M-": "MY", "HS-": "TH", "PK-": "ID", "RP-C": "PH",
    "B-": "CN", "JA": "JP", "HL": "KR", "VH-": "AU", "ZK-": "NZ",
    "PP-": "BR", "PR-": "BR", "PS-": "BR", "PT-": "BR", "LV-": "AR", "CC-": "CL",
    "HK-": "CO", "XA-": "MX", "XB-": "MX", "XC-": "MX",
    "ZS-": "ZA", "5N-": "NG", "ET-": "ET", "CN-": "MA", "TS-": "TN", "7T-": "DZ",
    "VP-B": "BM", "VQ-B": "BM", "P4-": "AW",
}
_REG_KEYS = sorted(REG_PREFIXES, key=len, reverse=True)


def reg_country(reg):
    """ISO-3166 alpha-2 country of registry, or None."""
    if not reg:
        return None
    reg = reg.upper().strip()
    for p in _REG_KEYS:
        if reg.startswith(p):
            return REG_PREFIXES[p]
    return None


def flag_emoji(iso2):
    if not iso2 or len(iso2) != 2 or not iso2.isalpha():
        return None
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in iso2.upper())


AIRLINE_CS = re.compile(r"^([A-Z]{3})\d")


def airline_for(callsign, own_op):
    m = AIRLINE_CS.match(callsign or "")
    if m and m.group(1) in AIRLINES:
        return AIRLINES[m.group(1)]
    return own_op or None

# ---------------------------------------------------------------- routes
# Departure/arrival airports are not in the ADS-B broadcast; they come from
# community callsign->route databases. Multiple providers are chained because
# each DB knows flights the others don't. Order via ROUTE_PROVIDERS env, e.g.
# ROUTE_PROVIDERS=adsbdb,hexdb (that's the default). Set
# DEBUG_ROUTES=1 to log every lookup to stderr.
ROUTE_CACHE = {}     # callsign -> (origin, destination) or None (missed everywhere)
AIRPORT_CACHE = {}   # icao -> airport dict or None
UA = {"User-Agent": "window-flights/1.0"}
DEBUG_ROUTES = os.environ.get("DEBUG_ROUTES") == "1"
MAX_NEW_LOOKUPS = 8  # new callsigns resolved per refresh; rest wait a cycle

ADSBDB_URL = "https://api.adsbdb.com/v0/callsign/{cs}"
HEXDB_ROUTE_URL = "https://hexdb.io/api/v1/route/icao/{cs}"
HEXDB_AIRPORT_URL = "https://hexdb.io/api/v1/airport/icao/{icao}"
ROUTESET_URL = "https://api.adsb.lol/api/0/routeset"


def _rlog(msg):
    if DEBUG_ROUTES:
        print(f"[routes] {msg}", file=sys.stderr, flush=True)


def _get_json(url):
    """GET -> parsed JSON; None on 404 (clean miss); raises on other errors."""
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _post_json(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={**UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _norm_airport(a, style):
    country = (a.get("countryiso2") or a.get("country_iso_name")
               or a.get("country_code") or a.get("country") or None)
    if country and len(country) != 2:
        country = None  # some sources put a full name here; keep ISO2 only
    if style == "adsbdb":
        return {"icao": a.get("icao_code"), "iata": a.get("iata_code"),
                "name": a.get("name"), "city": a.get("municipality"),
                "country": country,
                "lat": a.get("latitude"), "lon": a.get("longitude")}
    if style == "hexdb":
        return {"icao": a.get("icao"), "iata": a.get("iata"),
                "name": a.get("airport"), "city": a.get("region_name"),
                "country": country,
                "lat": a.get("latitude"), "lon": a.get("longitude")}
    # routeset / tar1090 style
    return {"icao": a.get("icao"), "iata": a.get("iata"),
            "name": a.get("name"), "city": a.get("location"),
            "country": country, "lat": a.get("lat"), "lon": a.get("lon")}


# ---- providers: each takes (callsign, lat, lon) and returns
#      (origin, destination) on success, None on a clean miss,
#      and raises on transport/format errors ----

def _p_adsbdb(cs, lat, lon):
    data = _get_json(ADSBDB_URL.format(cs=cs))
    if data is None:
        return None                       # 404 = unknown callsign
    fr = data.get("response") if isinstance(data, dict) else None
    fr = fr.get("flightroute") if isinstance(fr, dict) else None
    if isinstance(fr, dict) and fr.get("origin") and fr.get("destination"):
        return (_norm_airport(fr["origin"], "adsbdb"),
                _norm_airport(fr["destination"], "adsbdb"))
    return None                           # e.g. {"response": "unknown callsign"}


def _hexdb_airport(icao):
    if icao in AIRPORT_CACHE:
        return AIRPORT_CACHE[icao]
    ap = None
    try:
        data = _get_json(HEXDB_AIRPORT_URL.format(icao=icao))
        if isinstance(data, dict) and data.get("icao"):
            ap = _norm_airport(data, "hexdb")
    except Exception as e:
        _rlog(f"hexdb airport {icao}: {e}")
        return {"icao": icao, "iata": None, "name": None,
                "city": None, "country": None}   # usable stub, not cached
    AIRPORT_CACHE[icao] = ap
    return ap


def _p_hexdb(cs, lat, lon):
    data = _get_json(HEXDB_ROUTE_URL.format(cs=cs))
    if not isinstance(data, dict):
        return None
    codes = [c for c in (data.get("route") or "").split("-") if len(c) == 4]
    if len(codes) < 2:
        return None
    # multi-leg routes ("MMMX-KDFW-EDDF"): origin = first, destination = last
    o = _hexdb_airport(codes[0]) or {"icao": codes[0], "iata": None,
                                     "name": None, "city": None, "country": None}
    d = _hexdb_airport(codes[-1]) or {"icao": codes[-1], "iata": None,
                                      "name": None, "city": None, "country": None}
    return (o, d)


def _p_adsblol(cs, lat, lon):
    data = _post_json(ROUTESET_URL,
                      {"planes": [{"callsign": cs, "lat": lat, "lng": lon}]})
    items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        aps = item.get("_airports") or []
        if len(aps) >= 2:
            return (_norm_airport(aps[0], "routeset"),
                    _norm_airport(aps[-1], "routeset"))
    return None


ROUTE_STRICT = os.environ.get("ROUTE_STRICT") == "1"

PROVIDERS = {"adsbdb": _p_adsbdb, "hexdb": _p_hexdb, "adsblol": _p_adsblol}
# Default order chosen from live probing (2026-07): adsbdb is ~5x faster with
# fresher routes and proper city names; hexdb fills its gaps (e.g. BA Shuttle).
# adsb.lol's routeset endpoint returned non-JSON to every probe -> excluded;
# re-enable via ROUTE_PROVIDERS=adsbdb,hexdb,adsblol if it comes back.
PROVIDER_ORDER = [p.strip() for p in
                  os.environ.get("ROUTE_PROVIDERS", "adsbdb,hexdb").split(",")
                  if p.strip() in PROVIDERS]


def resolve_route(cs, lat, lon):
    """Try providers in order. Miss in one DB != miss overall.
    With ROUTE_STRICT=1, the first two answering providers must agree on the
    origin/destination ICAOs, else the route is treated as unknown."""
    missed_everywhere = True
    answers = []
    for name in PROVIDER_ORDER:
        try:
            result = PROVIDERS[name](cs, lat, lon)
        except Exception as e:
            _rlog(f"{name} {cs}: ERROR {e}")
            missed_everywhere = False     # transport error: retry next refresh
            continue
        if not result:
            _rlog(f"{name} {cs}: miss")
            continue
        _rlog(f"{name} {cs}: {result[0].get('icao')} -> {result[1].get('icao')}")
        if not ROUTE_STRICT:
            return result, True
        answers.append(result)
        if len(answers) == 2:
            a, b = answers
            if (a[0].get("icao") == b[0].get("icao")
                    and a[1].get("icao") == b[1].get("icao")):
                return a, True
            _rlog(f"{cs}: providers disagree "
                  f"({a[0].get('icao')}->{a[1].get('icao')} vs "
                  f"{b[0].get('icao')}->{b[1].get('icao')}), suppressed")
            return None, True
    if ROUTE_STRICT and len(answers) == 1:
        return answers[0], True           # only one DB knows it; can't cross-check
    return None, missed_everywhere


def lookup_routes(entries):
    """entries: [(callsign, lat, lon)] -> {callsign: route | None}.
    Only airline-style callsigns (AAA123) are looked up; registrations have
    no filed route in these databases."""
    new = 0
    for c, la, lo in entries:
        if not c or c in ROUTE_CACHE or not AIRLINE_CS.match(c):
            continue
        if new >= MAX_NEW_LOOKUPS:
            break
        new += 1
        result, definitive = resolve_route(c, la, lo)
        if result or definitive:
            ROUTE_CACHE[c] = result       # cache hits AND missed-everywhere
    if len(ROUTE_CACHE) > 2000:
        ROUTE_CACHE.clear()
    return {c: ROUTE_CACHE.get(c) for c, _, _ in entries}


# ---------------------------------------------------------------- geometry
def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def angle_diff(a, b):
    d = abs(a - b) % 360.0
    return d if d <= 180.0 else 360.0 - d


def elevation_deg(dist_km, alt_ft):
    alt_km = alt_ft * 0.0003048
    if dist_km < 0.01:
        return 90.0
    drop = dist_km ** 2 / (2 * EARTH_R_KM)
    return math.degrees(math.atan2(alt_km - drop, dist_km))


# ---------------------------------------------------------------- upstream
def fetch_upstream(source, lat, lon, range_km):
    nm = min(range_km / 1.852, 250)
    url = UPSTREAMS[source].format(lat=round(lat, 5), lon=round(lon, 5), nm=round(nm, 1))
    req = urllib.request.Request(url, headers={"User-Agent": "window-flights/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read()).get("ac") or []


def visible_aircraft(cfg, all_traffic=False):
    """Aircraft in the view cone, nearest first. With all_traffic=True,
    everything airborne within range is returned, flagged via in_view."""
    ac_list = fetch_upstream(cfg["source"], cfg["lat"], cfg["lon"], cfg["range_km"])
    half = cfg["fov"] / 2.0
    out = []
    for ac in ac_list:
        lat, lon = ac.get("lat"), ac.get("lon")
        if lat is None or lon is None:
            continue
        alt = ac.get("alt_geom", ac.get("alt_baro"))
        if alt == "ground":
            continue
        alt_ft = float(alt) if isinstance(alt, (int, float)) else None

        dist = haversine_km(cfg["lat"], cfg["lon"], lat, lon)
        if dist > cfg["range_km"]:
            continue
        brg = bearing_deg(cfg["lat"], cfg["lon"], lat, lon)
        # unknown altitude passes the elevation gate (can't judge it)
        elev = elevation_deg(dist, alt_ft) if alt_ft is not None else None
        in_view = (angle_diff(brg, cfg["bearing"]) <= half
                   and (elev is None or elev >= cfg["min_elev"]))
        if not in_view and not all_traffic:
            continue

        type_code = ac.get("t")
        callsign = (ac.get("flight") or "").strip() or ac.get("r") or ac.get("hex")
        out.append({
            "hex": ac.get("hex"),
            "callsign": callsign,
            "airline": airline_for(callsign, ac.get("ownOp")),
            "type": type_code,
            "type_desc": ac.get("desc") or TYPE_NAMES.get(type_code),
            "registration": ac.get("r"),
            "country": (country := reg_country(ac.get("r"))),
            "flag": flag_emoji(country),
            "in_view": in_view,
            "lat": lat,
            "lon": lon,
            "dist_km": round(dist, 2),
            "bearing_deg": round(brg, 1),
            "elevation_deg": round(elev, 1) if elev is not None else None,
            "alt_ft": alt_ft,
            "gs_kt": ac.get("gs"),
            "track_deg": ac.get("track"),
        })
    out.sort(key=lambda f: f["dist_km"])
    routes = lookup_routes([(f["callsign"], f["lat"], f["lon"]) for f in out])
    for f in out:
        r = routes.get(f["callsign"])
        f["origin"] = r[0] if r else None
        f["destination"] = r[1] if r else None
    return out


# ------------------------------------------------------------- prediction
# How far out to scan for approaching traffic, and how far ahead to look.
# A fast jet covers ~170 km in 10 min, so scan a wide radius but only keep
# aircraft that actually reach the (small) view cone within the horizon.
PREDICT_RANGE_KM = float(os.environ.get("PREDICT_RANGE_KM", "180"))
PREDICT_HORIZON_S = int(float(os.environ.get("PREDICT_HORIZON_MIN", "10")) * 60)
PREDICT_STEP_S = int(os.environ.get("PREDICT_STEP_S", "20"))
MAX_BOARD_FLIGHTS = int(os.environ.get("MAX_BOARD_FLIGHTS", "14"))


def project_position(lat, lon, track_deg, dist_km):
    """Great-circle destination point dist_km along heading track_deg."""
    d = dist_km / EARTH_R_KM
    b = math.radians(track_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d)
                   + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), math.degrees(l2)


def _entry_eta_s(cfg, lat, lon, alt_ft, gs_kt, track_deg, vs_fpm, half):
    """Seconds until the aircraft first enters the view cone (bearing within
    FOV, within range, high enough), extrapolating its current heading/speed/
    climb — or None if it never does within the horizon. Straight-line
    projection: good for a ~10 min look-ahead, approximate for turning traffic."""
    if not gs_kt or gs_kt < 30 or track_deg is None:
        return None
    for t in range(PREDICT_STEP_S, PREDICT_HORIZON_S + 1, PREDICT_STEP_S):
        dist_km = gs_kt * 1.852 * (t / 3600.0)   # kt -> km/h -> km in t s
        plat, plon = project_position(lat, lon, track_deg, dist_km)
        d = haversine_km(cfg["lat"], cfg["lon"], plat, plon)
        if d > cfg["range_km"]:
            continue
        if angle_diff(bearing_deg(cfg["lat"], cfg["lon"], plat, plon),
                      cfg["bearing"]) > half:
            continue
        a = (alt_ft + (vs_fpm or 0) * (t / 60.0)) if alt_ft is not None else None
        elev = elevation_deg(d, a) if a is not None else None
        if elev is None or elev >= cfg["min_elev"]:
            return t
    return None


def board_data(cfg):
    """In-view + soon-to-be-in-view aircraft for the display, each tagged with
    eta_min (0 = currently in view). Also returns the view-cone config so the
    board can draw the map."""
    ac_list = fetch_upstream(cfg["source"], cfg["lat"], cfg["lon"], PREDICT_RANGE_KM)
    half = cfg["fov"] / 2.0
    out = []
    for ac in ac_list:
        lat, lon = ac.get("lat"), ac.get("lon")
        if lat is None or lon is None:
            continue
        alt = ac.get("alt_geom", ac.get("alt_baro"))
        if alt == "ground":
            continue
        alt_ft = float(alt) if isinstance(alt, (int, float)) else None
        gs, track = ac.get("gs"), ac.get("track")
        vs = ac.get("geom_rate", ac.get("baro_rate"))
        dist = haversine_km(cfg["lat"], cfg["lon"], lat, lon)
        brg = bearing_deg(cfg["lat"], cfg["lon"], lat, lon)
        elev = elevation_deg(dist, alt_ft) if alt_ft is not None else None
        in_view = (dist <= cfg["range_km"]
                   and angle_diff(brg, cfg["bearing"]) <= half
                   and (elev is None or elev >= cfg["min_elev"]))
        eta_s = 0 if in_view else _entry_eta_s(cfg, lat, lon, alt_ft, gs, track, vs, half)
        if eta_s is None:
            continue
        type_code = ac.get("t")
        callsign = (ac.get("flight") or "").strip() or ac.get("r") or ac.get("hex")
        out.append({
            "hex": ac.get("hex"),
            "callsign": callsign,
            "airline": airline_for(callsign, ac.get("ownOp")),
            "type": type_code,
            "type_desc": ac.get("desc") or TYPE_NAMES.get(type_code),
            "in_view": in_view,
            "eta_min": round(eta_s / 60.0, 1),
            "bearing_deg": round(brg, 1),
            "dist_km": round(dist, 2),
            "track_deg": track,
            "alt_ft": alt_ft,
            "gs_kt": gs,
        })
    # in-view first, then soonest arrivals; keep the board readable
    out.sort(key=lambda f: (not f["in_view"], f["eta_min"], f["dist_km"]))
    out = out[:MAX_BOARD_FLIGHTS]
    routes = lookup_routes([(f["callsign"], f["bearing_deg"], f["dist_km"]) for f in out])
    for f in out:
        r = routes.get(f["callsign"])
        f["origin"] = r[0] if r else None
        f["destination"] = r[1] if r else None
    return {
        "config": {
            "bearing": cfg["bearing"],
            "fov": cfg["fov"],
            "range_km": cfg["range_km"],
            "predict_range_km": PREDICT_RANGE_KM,
            "horizon_min": PREDICT_HORIZON_S / 60.0,
        },
        "flights": out,
    }



# ---------------------------------------------------------------- byos
# TRMNL firmware endpoints, so a device pointed at this server (WiFi setup
# > Advanced > Custom Server > https://<your-host>) works with no cloud.
# The renderer (render_loop.py) keeps IMAGE_PATH fresh; /api/display hands
# the device its URL with a content-hash filename so unchanged screens
# skip the e-ink redraw entirely.
#
# Auth: /api/setup issues the device a deterministic api_key derived from its
# MAC and the secret DEVICE_SALT; the firmware echoes it back as the
# Access-Token header on every /api/display. We recompute and check it (no
# state to lose across restarts), and the rendered board is only reachable at
# an unguessable path derived from DEVICE_SALT — so the URL alone does not
# expose your board.
IMAGE_PATH = os.environ.get("RENDER_OUT", "latest.png")
REFRESH_RATE = int(os.environ.get("REFRESH_RATE", "60"))
DEVICE_SALT = os.environ.get("DEVICE_SALT", "window-flights")

if DEVICE_SALT == "window-flights":
    print("[byos] WARNING: DEVICE_SALT is unset (using the public default). "
          "Anyone who reads the source can derive your board URL and device "
          "keys. Set DEVICE_SALT to a long random secret before exposing this "
          "server to the internet.", file=sys.stderr, flush=True)


def _norm_mac(mac):
    """Normalise a MAC to bare uppercase hex so AA:BB.., aa-bb.., etc. compare
    equal."""
    return re.sub(r"[^0-9A-Fa-f]", "", mac or "").upper()


# Optional device allowlist. Empty (the default) means any device may pair —
# convenient for the very first pairing. Set ALLOWED_MACS to a comma-separated
# list of your device MAC(s) to refuse every other device outright.
ALLOWED_MACS = {_norm_mac(m) for m in os.environ.get("ALLOWED_MACS", "").split(",")
                if m.strip()}

if not ALLOWED_MACS:
    print("[byos] NOTE: ALLOWED_MACS is empty — any device that finds this "
          "server can pair. Set it to your device MAC to lock the board down.",
          file=sys.stderr, flush=True)


def _mac_allowed(mac):
    return (not ALLOWED_MACS) or (_norm_mac(mac) in ALLOWED_MACS)


def _device_key(mac):
    """Per-device api_key = HMAC(DEVICE_SALT, mac). Deterministic, so a
    correct device always presents the same Access-Token and we never need to
    persist anything."""
    return hmac.new(DEVICE_SALT.encode(), (mac or "").encode(),
                    hashlib.sha256).hexdigest()[:20]


# Unguessable, stable path the board image is served at (one board, shared by
# every authorised device). Only a device that passes /api/display auth is
# told this URL.
SCREEN_TOKEN = hmac.new(DEVICE_SALT.encode(), b"screen-image",
                        hashlib.sha256).hexdigest()[:24]
SCREEN_PATH = f"/screen/{SCREEN_TOKEN}.png"


def _image_state():
    """(exists, content-hash filename token)"""
    try:
        with open(IMAGE_PATH, "rb") as fh:
            digest = hashlib.sha1(fh.read()).hexdigest()[:12]
        return True, f"board-{digest}"
    except OSError:
        return False, "not-ready"


def byos_setup(mac, base_url):
    return {
        "status": 200,
        "api_key": _device_key(mac),
        "friendly_id": "PLANES" + (mac or "").replace(":", "")[-4:].upper(),
        "image_url": base_url + SCREEN_PATH,
        "message": "Welcome aboard — overhead board ready.",
    }


def byos_display(base_url):
    ready, token = _image_state()
    return {
        "status": 0,
        "image_url": base_url + SCREEN_PATH,
        "image_url_timeout": 0,
        "filename": token,
        "refresh_rate": REFRESH_RATE if ready else 10,  # poll fast until first render
        "update_firmware": False,
        "firmware_url": None,
        "reset_firmware": False,
        # MUST be "none": the firmware persists this value and only runs the
        # image download/display path while it is SF_NONE. Sending "sleep"
        # here parks the device in the sleep special-function branch, so it
        # stops refreshing the board (shows "Full view not available").
        "special_function": "none",
    }


def byos_reset(message):
    """Sent when the Access-Token is missing/invalid: tells a desynced device
    to re-pair, and hands an attacker no image_url."""
    return {
        "status": 500,
        "reset_firmware": False,
        "image_url": None,
        "refresh_rate": 60,
        "special_function": "sleep",
        "error": message,
    }


# Only these static files are served directly; everything else (the .py
# source, the raw image file, dotfiles) is 404'd so a public URL can't be
# scraped for the board or the code.
STATIC_WHITELIST = {"/window-flights.html", "/trmnl-board.html"}


# ---------------------------------------------------------------- server
class Handler(SimpleHTTPRequestHandler):
    def _public_base(self):
        """External base URL, honouring a TLS-terminating proxy (Fly, etc.).
        The firmware won't follow 301/302 for image fetches, so the scheme we
        hand back must already be the real one (https behind the proxy)."""
        proto = (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip()
        host = (self.headers.get("X-Forwarded-Host")
                or self.headers.get("Host") or "localhost")
        return f"{proto}://{host}"

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.path = "/window-flights.html"
            return super().do_GET()
        if parsed.path == "/api/visible":
            return self.handle_api(parsed)
        if parsed.path == "/api/board":
            return self.handle_board()
        if parsed.path in ("/api/setup", "/api/setup/"):
            mac = self.headers.get("ID", "unknown")
            if not _mac_allowed(mac):
                print(f"[byos] setup REFUSED for unlisted device {mac}", flush=True)
                return self._json(200, {"status": 404, "api_key": None,
                                        "friendly_id": None, "image_url": None,
                                        "message": "device not authorised"})
            print(f"[byos] setup from device {mac}", flush=True)
            return self._json(200, byos_setup(mac, self._public_base()))
        if parsed.path in ("/api/display", "/api/display/"):
            mac = self.headers.get("ID", "?")
            token = self.headers.get("Access-Token") or self.headers.get("access-token")
            batt = self.headers.get("BATTERY_VOLTAGE") or self.headers.get("Battery-Voltage")
            rssi = self.headers.get("RSSI") or self.headers.get("Rssi")
            if not _mac_allowed(mac) or not hmac.compare_digest(token or "", _device_key(mac)):
                print(f"[byos] display DENIED for {mac} (unlisted or bad token)", flush=True)
                return self._json(200, byos_reset("unrecognised device"))
            print(f"[byos] display poll from {mac} batt={batt} rssi={rssi}", flush=True)
            return self._json(200, byos_display(self._public_base()))
        if parsed.path == SCREEN_PATH:
            return self.serve_screen()
        if parsed.path == "/proxy":
            return self.handle_proxy(parsed)
        if parsed.path in STATIC_WHITELIST:
            return super().do_GET()
        return self._json(404, {"error": "not found"})

    def serve_screen(self):
        try:
            with open(IMAGE_PATH, "rb") as fh:
                data = fh.read()
        except OSError:
            return self._json(404, {"error": "no board rendered yet"})
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _is_local(self):
        """True only for same-container callers (the board renderer). External
        traffic arrives via the hosting proxy with an X-Forwarded-For header."""
        if self.headers.get("X-Forwarded-For"):
            return False
        return self.client_address[0] in ("127.0.0.1", "::1", "localhost")

    def handle_board(self):
        # The board is rendered locally and uses your configured home location,
        # so this is local-only (like the default /api/visible).
        if not self._is_local():
            return self._json(403, {"error": "board data is local-only"})
        try:
            data = board_data(dict(DEFAULTS))
        except Exception as e:
            return self._json(502, {"error": f"upstream {DEFAULTS['source']} failed: {e}"})
        return self._json(200, data)

    def handle_api(self, parsed):
        q = parse_qs(parsed.query)
        # The no-parameter default is your configured home location — only hand
        # that out to the local board renderer. External callers must pass their
        # own lat/lon so a public URL can't be used to infer where you are.
        if not self._is_local() and not ("lat" in q and "lon" in q):
            return self._json(400, {"error": "lat and lon query params required"})
        cfg = dict(DEFAULTS)
        try:
            for key in ("lat", "lon", "bearing", "fov", "range_km", "min_elev"):
                if key in q:
                    cfg[key] = float(q[key][0])
            if "source" in q:
                cfg["source"] = q["source"][0]
        except ValueError:
            return self._json(400, {"error": "numeric parameter expected"})
        if cfg["source"] not in UPSTREAMS:
            return self._json(400, {"error": f"unknown source '{cfg['source']}'"})
        cfg["range_km"] = min(max(cfg["range_km"], 1), 463)  # ≤250 nm
        all_traffic = q.get("all", ["0"])[0].lower() in ("1", "true", "yes")
        try:
            aircraft = visible_aircraft(cfg, all_traffic=all_traffic)
        except Exception as e:
            return self._json(502, {"error": f"upstream {cfg['source']} failed: {e}"})
        self._json(200, aircraft)

    def handle_proxy(self, parsed):
        q = parse_qs(parsed.query)
        try:
            src = q["src"][0]
            lat, lon = float(q["lat"][0]), float(q["lon"][0])
            nm = min(float(q["nm"][0]), 250)
        except (KeyError, ValueError, IndexError):
            return self._json(400, {"error": "expected src, lat, lon, nm"})
        template = UPSTREAMS.get(src)
        if template is None:
            return self._json(400, {"error": f"unknown src '{src}'"})
        url = template.format(lat=round(lat, 5), lon=round(lon, 5), nm=round(nm, 1))
        req = urllib.request.Request(url, headers={"User-Agent": "window-flights/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read()
        except Exception as e:
            return self._json(502, {"error": f"upstream {src} failed: {e}"})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if urlparse(self.path).path in ("/api/log", "/api/logs", "/api/logs/"):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)[:500] if length else b""
            print(f"[byos] device log: {body.decode(errors='replace')}", flush=True)
            return self._json(200, {"status": 200})
        return self._json(404, {"error": "not found"})

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # args[0] is usually the request line but can be an HTTPStatus enum
        # (error logging path) — coerce before substring checks.
        first = str(args[0]) if args else ""
        if "/proxy" in first or "/api/" in first:
            super().log_message(fmt, *args)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (0.0.0.0 to allow other machines)")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    print(f"Webapp : http://{a.host}:{a.port}/")
    print(f"API    : http://{a.host}:{a.port}/api/visible")
    HTTPServer((a.host, a.port), Handler).serve_forever()
