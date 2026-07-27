#!/usr/bin/env python3
"""
serve.py — local server for window-flights.html + machine-readable API

Endpoints:
  /                 the webapp
  /proxy?...        raw upstream passthrough (used by the webapp)
  /api/visible      JSON array of aircraft currently in the view cone

/api/visible with no parameters uses the built-in window defaults
(135° SE, 145° FOV, 5 km, min elevation 8° from the configured location). Every
parameter can be overridden:

  /api/visible?lat=51.5074&lon=-0.1278&bearing=135&fov=145&range_km=5&min_elev=8&source=adsblol

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
import time
import hmac
import hashlib
import urllib.request
import urllib.error
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
    "bearing": 135.0,
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
ROUTE_CACHE = {}     # callsign -> route dict or None (missed everywhere)
AIRPORT_CACHE = {}   # icao -> airport dict or None
UA = {"User-Agent": "window-flights/1.0"}
DEBUG_ROUTES = os.environ.get("DEBUG_ROUTES") == "1"
MAX_NEW_LOOKUPS = int(os.environ.get("ROUTE_MAX_NEW", "8"))  # per refresh
ROUTE_BUDGET_S = float(os.environ.get("ROUTE_BUDGET_S", "8"))  # per refresh

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

def _iata_flight_no(fr):
    """IATA flight number (BA172) from an adsbdb flightroute.

    callsign_iata is normally the whole IATA callsign, but handle the case of a
    source returning only the numeric part by prefixing the airline's IATA
    code — the airline object is in the same response either way."""
    num = (fr.get("callsign_iata") or "").strip().upper()
    if not num:
        return None
    code = ((fr.get("airline") or {}).get("iata") or "").strip().upper()
    return code + num if (num[:1].isdigit() and code) else num


def _p_adsbdb(cs, lat, lon):
    data = _get_json(ADSBDB_URL.format(cs=cs))
    if data is None:
        return None                       # 404 = unknown callsign
    fr = data.get("response") if isinstance(data, dict) else None
    fr = fr.get("flightroute") if isinstance(fr, dict) else None
    if isinstance(fr, dict) and fr.get("origin") and fr.get("destination"):
        return {"origin": _norm_airport(fr["origin"], "adsbdb"),
                "destination": _norm_airport(fr["destination"], "adsbdb"),
                "flight_no": _iata_flight_no(fr),
                "airline": ((fr.get("airline") or {}).get("name")) or None}
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
    return {"origin": o, "destination": d, "flight_no": None, "airline": None}


def _p_adsblol(cs, lat, lon):
    data = _post_json(ROUTESET_URL,
                      {"planes": [{"callsign": cs, "lat": lat, "lng": lon}]})
    items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        aps = item.get("_airports") or []
        if len(aps) >= 2:
            return {"origin": _norm_airport(aps[0], "routeset"),
                    "destination": _norm_airport(aps[-1], "routeset"),
                    "flight_no": None, "airline": None}
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
        _rlog(f"{name} {cs}: {result['origin'].get('icao')} -> "
              f"{result['destination'].get('icao')}")
        if not ROUTE_STRICT:
            return result, True
        answers.append(result)
        if len(answers) == 2:
            a, b = answers
            if (a["origin"].get("icao") == b["origin"].get("icao")
                    and a["destination"].get("icao") == b["destination"].get("icao")):
                # keep whichever answer carried a flight number
                return (a if a.get("flight_no") else b), True
            _rlog(f"{cs}: providers disagree "
                  f"({a['origin'].get('icao')}->{a['destination'].get('icao')} vs "
                  f"{b['origin'].get('icao')}->{b['destination'].get('icao')}), suppressed")
            return None, True
    if ROUTE_STRICT and len(answers) == 1:
        return answers[0], True           # only one DB knows it; can't cross-check
    return None, missed_everywhere


def lookup_routes(entries):
    """entries: [(callsign, lat, lon)] -> {callsign: route | None}.
    Only airline-style callsigns (AAA123) are looked up; registrations have
    no filed route in these databases.

    Unlike the FR24 path this is one request per callsign, so it is bounded
    twice over: at most MAX_NEW_LOOKUPS new callsigns per refresh, and a wall
    -clock budget, since a render must not stall behind a slow provider. What
    doesn't resolve this cycle is picked up on the next one."""
    new, deadline = 0, time.time() + ROUTE_BUDGET_S
    for c, la, lo in entries:
        if not c or c in ROUTE_CACHE or not AIRLINE_CS.match(c):
            continue
        if new >= MAX_NEW_LOOKUPS or time.time() > deadline:
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


def enu_km(lat, lon, home_lat, home_lon):
    """East/north offset in km from home (equirectangular). Exact enough over
    the tens of km the prediction works in, and being a flat frame it lets the
    approach be solved as plane geometry."""
    east = math.radians(lon - home_lon) * math.cos(math.radians(home_lat)) * EARTH_R_KM
    north = math.radians(lat - home_lat) * EARTH_R_KM
    return east, north


# ---------------------------------------------------------------- upstream
# Positions come from the free ADS-B feeds (accurate, unlimited, frequent) and
# drive the map + prediction. Routes come from the free community databases
# above — adsbdb carries the IATA flight number as well as the airports, which
# together are everything the board displays.
#
# FlightRadar24 (metered, first-party) is now only a gap-filler for the
# callsigns those databases don't know. It bills 8 credits per returned flight
# against a 30k/month Explorer tier, so it is asked last, for the leftovers,
# and only under a monthly safety cap — past that we degrade gracefully to no
# route. Leaving F24_KEY unset runs the whole board for free.
F24_KEY = os.environ.get("F24_KEY")
FR24_BASE = "https://fr24api.flightradar24.com"
FR24_MONTHLY_CAP = int(os.environ.get("FR24_MONTHLY_CREDITS", "29000"))
FR24_ROUTE_TTL = float(os.environ.get("FR24_ROUTE_TTL", "3600"))
FR24_ENRICH_MAX = int(os.environ.get("FR24_ENRICH_MAX", "14"))  # legacy alias
# Board flights enriched per render, whichever source answers.
ENRICH_MAX = int(os.environ.get("ROUTE_ENRICH_MAX", str(FR24_ENRICH_MAX)))
FR24_CREDIT_PER_FLIGHT = 8
_fr24_routes = {}   # callsign -> (ts, route dict | None)

# Live diagnostics surfaced on the board's debug panel.
# credits_est = our local running tally since restart (always known);
# credits_used = the real monthly figure from FR24 /api/usage (best-effort).
DIAG = {"source": None, "n_scan": 0, "n_enriched": 0, "n_fr24": 0, "n_turn": 0,
        "fetched_at": None,
        "error": None, "credits_used": None, "credits_est": 0,
        "credits_cap": FR24_MONTHLY_CAP, "credits_at": None, "fr24": bool(F24_KEY)}


def _fr24_get(path, params=""):
    req = urllib.request.Request(FR24_BASE + path + params, headers={
        "Authorization": f"Bearer {F24_KEY}",
        "Accept": "application/json", "Accept-Version": "v1",
        "User-Agent": "window-flights/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def fr24_usage():
    """Refresh real monthly credit usage for the debug panel (rate-limited)."""
    now = time.time()
    if not F24_KEY:
        return
    if DIAG["credits_at"] and now - DIAG["credits_at"] < 300:
        return
    try:
        d = _fr24_get("/api/usage", "?period=30d")
        DIAG["credits_used"] = sum(int(r.get("credits") or 0) for r in (d.get("data") or []))
    except Exception:
        pass
    DIAG["credits_at"] = now


def _fr24_budget_ok():
    # guard on the higher of the real monthly figure and our local estimate
    used = max(DIAG.get("credits_used") or 0, DIAG.get("credits_est") or 0)
    return used < FR24_MONTHLY_CAP


def fr24_fetch_routes(callsigns):
    """Fetch routes for up to 15 callsigns in ONE FR24 call (respects the
    10 req/min limit; costs 8 credits per flight returned). Returns
    {callsign: route|None}."""
    out = {}
    if not F24_KEY or not callsigns:
        return out
    cs = ",".join(callsigns[:15])
    try:
        d = _fr24_get("/api/live/flight-positions/full", f"?callsigns={cs}")
        recs = d.get("data") or []
        for r in recs:
            c = (r.get("callsign") or "").strip()
            if not c:
                continue
            out[c] = {
                "orig_iata": r.get("orig_iata"), "orig_icao": r.get("orig_icao"),
                "dest_iata": r.get("dest_iata"), "dest_icao": r.get("dest_icao"),
                "airline_code": r.get("operating_as") or r.get("painted_as"),
                "eta": r.get("eta"), "flight_no": r.get("flight"),
                "type": r.get("type"), "reg": r.get("reg"),
            }
        DIAG["credits_est"] += FR24_CREDIT_PER_FLIGHT * max(1, len(recs))
    except urllib.error.HTTPError as e:
        DIAG["error"] = "FR24: out of credits (402)" if e.code == 402 else f"FR24 HTTP {e.code}"
        if e.code == 402:
            DIAG["credits_used"] = FR24_MONTHLY_CAP
    except Exception as e:
        DIAG["error"] = f"FR24 {type(e).__name__}"
    return out


def _norm_adsb(r):
    alt = r.get("alt_geom", r.get("alt_baro"))
    return {
        "lat": r.get("lat"), "lon": r.get("lon"),
        "alt_ft": float(alt) if isinstance(alt, (int, float)) else None,
        "gs_kt": r.get("gs"), "track_deg": r.get("track"),
        "vs_fpm": r.get("geom_rate", r.get("baro_rate")),
        # bank angle and rate of turn, when the feed carries them — these turn
        # a straight-line guess into an arc for manoeuvring traffic
        "roll": r.get("roll"), "track_rate": r.get("track_rate"),
        "type": r.get("t"), "callsign": (r.get("flight") or "").strip(),
        "flight_no": None, "reg": r.get("r"), "hex": r.get("hex"),
        "airline_code": r.get("ownOp"), "squawk": r.get("squawk"),
    }


def fetch_aircraft(source, lat, lon, range_km):
    """Wide positional scan from the free ADS-B feed (unlimited). Returns
    normalized records; routes are added later by FR24 enrichment."""
    try:
        nm = min(range_km / 1.852, 250)
        url = UPSTREAMS[source].format(lat=round(lat, 5), lon=round(lon, 5), nm=round(nm, 1))
        req = urllib.request.Request(url, headers={"User-Agent": "window-flights/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            ac = json.loads(resp.read()).get("ac") or []
        recs = [_norm_adsb(a) for a in ac
                if a.get("lat") is not None and a.get("lon") is not None
                and a.get("alt_geom", a.get("alt_baro")) != "ground"]
        DIAG.update(source="ADSB+DB+FR24" if F24_KEY else "ADSB+DB",
                    n_scan=len(recs), fetched_at=time.time(), error=None)
        return recs
    except Exception as e:
        DIAG.update(error=f"ADSB {type(e).__name__}", n_scan=0, fetched_at=time.time())
        return []


def enrich_routes(flights):
    """Fill origin/destination/flight number/airline on the board's flights.

    The free community route databases are the primary source: no key, no
    quota, and adsbdb carries the IATA flight number alongside the route.
    FR24, when a key is configured, is spent only on the callsigns the free
    chain could not resolve — it bills 8 credits per flight, so it earns its
    place as a gap-filler rather than the default. Everything is cached hard
    by callsign, so a given flight costs at most one lookup."""
    board = flights[:ENRICH_MAX]
    routes = lookup_routes([(f.get("callsign"), f.get("lat"), f.get("lon"))
                            for f in board])

    n_free, unresolved = 0, []
    for f in board:
        rt = routes.get(f.get("callsign"))
        if not rt:
            if f.get("callsign"):
                unresolved.append(f)
            continue
        f["origin"] = rt["origin"]
        f["destination"] = rt["destination"]
        if rt.get("flight_no"):
            f["flight_no"] = rt["flight_no"]
        # keep the curated short name from AIRLINES when we already have one
        if rt.get("airline") and not f.get("airline"):
            f["airline"] = rt["airline"]
        n_free += 1

    n_fr24 = 0
    if unresolved and F24_KEY and _fr24_budget_ok():
        now = time.time()
        need = [f["callsign"] for f in unresolved
                if not (_fr24_routes.get(f["callsign"])
                        and now - _fr24_routes[f["callsign"]][0] < FR24_ROUTE_TTL)]
        if need:
            fetched = fr24_fetch_routes(need)
            for c in need:
                _fr24_routes[c] = (now, fetched.get(c))  # cache misses too
        for f in unresolved:
            hit = _fr24_routes.get(f.get("callsign"))
            rt = hit[1] if hit else None
            if not rt:
                continue
            f["origin"] = airport_info(rt.get("orig_iata"), rt.get("orig_icao"))
            f["destination"] = airport_info(rt.get("dest_iata"), rt.get("dest_icao"))
            f["eta"] = rt.get("eta")
            if rt.get("flight_no"):
                f["flight_no"] = rt["flight_no"]
            if rt.get("airline_code"):
                f["airline"] = airline_for(f.get("callsign"), rt["airline_code"])
            n_fr24 += 1

    DIAG["n_enriched"] = n_free + n_fr24
    DIAG["n_fr24"] = n_fr24
    return flights


def airport_info(iata, icao):
    """{iata, icao, city, country} for an airport code, enriched (and cached)
    from the free airport reference DB. FR24 gives us the accurate codes; this
    only adds the human-readable city/country label."""
    if not (iata or icao):
        return None
    info = {"iata": iata, "icao": icao, "city": None, "country": None}
    if icao:
        ap = _hexdb_airport(icao)
        if ap:
            info["city"] = ap.get("city")
            info["country"] = ap.get("country")
            info["iata"] = iata or ap.get("iata")
    return info


def _flight_record(ac, cfg, dist, brg, elev, in_view):
    """Per-aircraft record from a normalized ADS-B entry. origin/destination/
    eta start empty and are filled by FR24 enrichment for the final short list."""
    callsign = ac["callsign"] or ac.get("reg") or ac.get("hex")
    return {
        "hex": ac.get("hex"),
        "callsign": callsign,
        "flight_no": ac.get("flight_no"),
        "airline": airline_for(callsign, ac.get("airline_code")),
        "type": ac.get("type"),
        "type_desc": TYPE_NAMES.get(ac.get("type")),
        "registration": ac.get("reg"),
        "in_view": in_view,
        "lat": ac["lat"], "lon": ac["lon"],
        "dist_km": round(dist, 2),
        "bearing_deg": round(brg, 1),
        "elevation_deg": round(elev, 1) if elev is not None else None,
        "alt_ft": ac.get("alt_ft"),
        "gs_kt": ac.get("gs_kt"),
        "track_deg": ac.get("track_deg"),
        "vs_fpm": ac.get("vs_fpm"),
        "squawk": ac.get("squawk"),
        "eta": None,
        "miss_km": None,      # set by board_data for predicted arrivals
        "turn_dps": None,     # rate of turn, deg/s, + = right
        "turning_for": None,  # IATA of the field it is banking towards
        "origin": None,
        "destination": None,
    }


def visible_aircraft(cfg, all_traffic=False):
    """Aircraft in the view cone, nearest first. With all_traffic=True,
    everything airborne within range is returned, flagged via in_view."""
    ac_list = fetch_aircraft(cfg["source"], cfg["lat"], cfg["lon"], cfg["range_km"])
    half = cfg["fov"] / 2.0
    out = []
    for ac in ac_list:
        lat, lon = ac["lat"], ac["lon"]
        alt_ft = ac.get("alt_ft")
        dist = haversine_km(cfg["lat"], cfg["lon"], lat, lon)
        if dist > cfg["range_km"]:
            continue
        brg = bearing_deg(cfg["lat"], cfg["lon"], lat, lon)
        elev = elevation_deg(dist, alt_ft) if alt_ft is not None else None
        in_view = (angle_diff(brg, cfg["bearing"]) <= half
                   and (elev is None or elev >= cfg["min_elev"]))
        if not in_view and not all_traffic:
            continue
        out.append(_flight_record(ac, cfg, dist, brg, elev, in_view))
    out.sort(key=lambda f: f["dist_km"])
    enrich_routes(out)
    return out


# ------------------------------------------------------------- prediction
# How far out to scan for approaching traffic, and how far ahead to look.
#
# The scan radius is set by how accurate a straight-line projection can be, not
# by how far an aircraft could theoretically travel. To pass within RANGE_KM
# (5 km) of home, an aircraft's heading has to be right to within
# atan(range/distance): +/-9.5 deg from 30 km, but only +/-2 deg from 140 km —
# tighter than the turns terminal traffic makes constantly, so distant
# predictions are noise. 30 km keeps the tolerance near +/-10 deg, and the
# horizon is set to match: 30 km at approach speeds (180-250 kt) is ~4 min.
PREDICT_RANGE_KM = float(os.environ.get("PREDICT_RANGE_KM", "30"))
PREDICT_HORIZON_S = int(float(os.environ.get("PREDICT_HORIZON_MIN", "4")) * 60)
# Sampling interval for the *angular* checks only; the range crossing itself is
# solved in closed form (see _entry_prediction), so this no longer decides
# whether a crossing is detected at all.
PREDICT_STEP_S = int(os.environ.get("PREDICT_STEP_S", "20"))
MAX_BOARD_FLIGHTS = int(os.environ.get("MAX_BOARD_FLIGHTS", "14"))

# Drop predictions whose closest approach is farther than this. Defaults to
# "off" (any track that enters the range circle counts); lower it toward 0 to
# keep only aircraft predicted to pass more or less overhead.
PREDICT_MISS_MAX_KM = float(os.environ.get("PREDICT_MISS_MAX_KM", "0") or 0) or None

# Predictions are re-derived from scratch every render, so one noisy projection
# could flash a flight onto the board and drop it again. Require this many
# consecutive renders predicting the same aircraft before it earns a row. Set
# to 1 to disable. Aircraft actually in view are never held back.
PREDICT_CONFIRM = max(1, int(os.environ.get("PREDICT_CONFIRM", "3")))
# How far apart renders are, so the gate can reason in wall-clock rather
# than in renders. Mirrors render_loop.py's RENDER_INTERVAL.
RENDER_INTERVAL_HINT = float(os.environ.get("RENDER_INTERVAL", "30"))
_predict_streak = {}   # hex -> consecutive renders this aircraft was predicted


# ---- turning traffic -------------------------------------------------------
# A straight-line projection is what makes distant predictions unreliable, and
# terminal traffic turns constantly. When the feed reports bank angle or rate
# of turn we can do better: fly the aircraft round its actual arc instead.
#
# Neither field is guaranteed — plenty of aircraft report neither — so every
# consumer below degrades to the straight-line path when turn data is absent.
# DIAG["n_turn"] counts how many of the scanned aircraft carried it, so the
# board itself answers whether your feed supplies it.
TURN_MIN_DPS = float(os.environ.get("TURN_MIN_DPS", "0.4"))   # below this = straight
G_MS2 = 9.80665


def turn_rate_dps(roll_deg, gs_kt, track_rate):
    """Rate of turn in deg/s, positive = to the right, or None if unknown.

    Prefers the feed's own track_rate; otherwise derives it from bank angle
    and ground speed via the coordinated-turn relation w = g*tan(bank)/V."""
    if track_rate is not None:
        try:
            return float(track_rate)
        except (TypeError, ValueError):
            pass
    if roll_deg is None or not gs_kt:
        return None
    try:
        phi, v = math.radians(float(roll_deg)), float(gs_kt) * 0.514444  # kt->m/s
    except (TypeError, ValueError):
        return None
    if v < 20 or abs(phi) > math.radians(60):     # implausible bank: ignore
        return None
    return math.degrees(G_MS2 * math.tan(phi) / v)


def _arc_xy(e0, n0, speed_kms, track_deg, omega_dps, t):
    """Position t seconds along a constant-rate turn, in the flat frame.
    Integrating a heading that rotates at a constant rate gives a circle of
    radius V/w; omega_dps == 0 degenerates to the straight-line case."""
    th0 = math.radians(track_deg)
    if abs(omega_dps) < 1e-6:
        return e0 + speed_kms * math.sin(th0) * t, n0 + speed_kms * math.cos(th0) * t
    w = math.radians(omega_dps)
    r = speed_kms / w
    th = th0 + w * t
    return (e0 + r * (math.cos(th0) - math.cos(th)),
            n0 + r * (math.sin(th) - math.sin(th0)))


# Airports close enough that traffic turning onto their approach passes the
# window. Used only to name what an aircraft is turning towards.
LOCAL_AIRPORTS = {
    "LHR": (51.4700, -0.4543), "LCY": (51.5053, 0.0553),
    "LGW": (51.1481, -0.1903), "STN": (51.8850, 0.2350),
    "LTN": (51.8747, -0.3683),
}


# An aircraft manoeuvring anywhere near London is turning "towards" one of
# these fields most of the time, so the claim only means something if it is
# close enough and low enough to actually be joining the approach.
APPROACH_MAX_KM = float(os.environ.get("APPROACH_MAX_KM", "45"))
APPROACH_MAX_FT = float(os.environ.get("APPROACH_MAX_FT", "12000"))


def approach_turn(lat, lon, alt_ft, track_deg, omega_dps, vs_fpm):
    """Which local airport, if any, this aircraft is banking onto.

    Requires a descending turn, below approach altitude, within range of the
    field, actively closing the angle between its track and the bearing to
    that field, and rolling out pointing at it inside a couple of minutes.
    Where several qualify the nearest wins — a jet 6 km off London City is a
    far better bet than one 40 km from Luton on a similar heading."""
    if omega_dps is None or abs(omega_dps) < TURN_MIN_DPS or track_deg is None:
        return None
    if vs_fpm is None or vs_fpm > -200:      # arrivals descend; ignore climbs
        return None
    if alt_ft is None or alt_ft > APPROACH_MAX_FT:
        return None
    best, best_dist = None, None
    for code, (alat, alon) in LOCAL_AIRPORTS.items():
        dist = haversine_km(lat, lon, alat, alon)
        if dist > APPROACH_MAX_KM:
            continue
        brg = bearing_deg(lat, lon, alat, alon)
        # signed track error, positive when the field lies to the right
        d = ((brg - track_deg + 180) % 360) - 180
        if abs(d) < 5 or abs(d) > 150:       # already aligned, or facing away
            continue
        if (d > 0) != (omega_dps > 0):       # turning the other way
            continue
        if abs(d) / abs(omega_dps) > 150:    # would not roll out for minutes
            continue
        if best_dist is None or dist < best_dist:
            best, best_dist = code, dist
    return best


def _cpa(e0, n0, ve, vn, radius_km):
    """Closest approach of a straight track to the circle of radius radius_km
    centred on home, worked in the flat east/north frame.

    Returns (t_enter, t_exit, miss_km), times in seconds from now. t_enter is
    None when the track never reaches the circle, in which case miss_km still
    reports by how far it is predicted to miss."""
    speed2 = ve * ve + vn * vn
    if speed2 <= 0:
        return None, None, None
    # |r0 + v t| is minimised where its derivative vanishes: t = -(r0.v)/|v|^2
    t_cpa = -(e0 * ve + n0 * vn) / speed2
    miss = math.hypot(e0 + ve * t_cpa, n0 + vn * t_cpa)
    if miss > radius_km:
        return None, None, miss
    # |r0 + v t| = radius has two roots, sitting either side of t_cpa
    half_chord = math.sqrt(max(0.0, radius_km ** 2 - miss ** 2) / speed2)
    return t_cpa - half_chord, t_cpa + half_chord, miss


def _entry_prediction(cfg, lat, lon, alt_ft, gs_kt, track_deg, vs_fpm, half,
                      omega_dps=None):
    """(seconds until the aircraft enters the view cone, predicted miss
    distance in km, seconds it then spends inside). eta is None when it never
    enters within the horizon.

    Wings level, reaching the range circle is the binding constraint — with a
    wide FOV almost anything that gets within range is inside the bearing wedge
    too — so that crossing is solved in closed form, and the angular checks run
    only across the interval already known to be in range.

    In a turn there is no such closed form, so the arc is walked instead. That
    is the point: rather than projecting a straight line the aircraft is not
    flying and being wrong, fly it round the circle its bank angle implies.

    miss_km doubles as a confidence signal: a track predicted to pass 0.5 km
    away is near-certain, one grazing the edge of the circle is a coin flip.
    The dwell is what makes a brief pass distinguishable from a long one — a
    crossing shorter than the confirmation window can never be confirmed
    before it is over."""
    if not gs_kt or gs_kt < 30 or track_deg is None:
        return None, None, None
    e0, n0 = enu_km(lat, lon, cfg["lat"], cfg["lon"])
    speed = gs_kt * 1.852 / 3600.0          # kt -> km/s
    turning = omega_dps is not None and abs(omega_dps) >= TURN_MIN_DPS

    def visible_at(t):
        """(in the cone?, distance km) at t seconds along the projected path."""
        e, n = _arc_xy(e0, n0, speed, track_deg, omega_dps or 0.0, t)
        d = math.hypot(e, n)
        if d > cfg["range_km"]:
            return False, d
        brg = (math.degrees(math.atan2(e, n)) + 360.0) % 360.0
        if angle_diff(brg, cfg["bearing"]) > half:
            return False, d
        a = (alt_ft + (vs_fpm or 0) * (t / 60.0)) if alt_ft is not None else None
        elev = elevation_deg(d, a) if a is not None else None
        return (elev is None or elev >= cfg["min_elev"]), d

    if turning:
        # Constant-rate arc: no closed form, so sample it finely. Only turning
        # traffic pays this cost, and it is a handful of aircraft per render.
        step, enter, leave, miss = 5.0, None, None, None
        t = 0.0
        while t <= PREDICT_HORIZON_S:
            ok, d = visible_at(t)
            miss = d if miss is None else min(miss, d)
            if ok:
                if enter is None:
                    enter = t
                leave = t
            t += step
        if enter is None:
            return None, miss, None
        return enter, miss, (leave - enter) + step

    ve = speed * math.sin(math.radians(track_deg))
    vn = speed * math.cos(math.radians(track_deg))
    t_enter, t_exit, miss = _cpa(e0, n0, ve, vn, cfg["range_km"])
    if t_enter is None or t_exit <= 0 or t_enter > PREDICT_HORIZON_S:
        return None, miss, None
    # Walk the in-range interval for the first moment it is also inside the
    # wedge and high enough to clear the skyline.
    t0, t1 = max(t_enter, 0.0), min(t_exit, float(PREDICT_HORIZON_S))
    steps = max(1, min(12, int((t1 - t0) / PREDICT_STEP_S) + 1))
    for i in range(steps + 1):
        t = t0 + (t1 - t0) * i / steps
        ok, _ = visible_at(t)
        if ok:
            return t, miss, max(0.0, t1 - t)
    return None, miss, None


def _confirmations_for(f):
    """How many consecutive renders this prediction must survive.

    The full count is right for a flight predicted minutes out — that is where
    a straight-line projection is least trustworthy and most worth filtering.
    It is arithmetically impossible for a brief pass: a crossing that starts in
    40 s and lasts 18 s is over long before three renders 30 s apart can agree
    on it. So when the whole event fits inside the confirmation window, ask for
    one sighting instead and let it through."""
    if PREDICT_CONFIRM <= 1:
        return 1
    eta, dwell = f.get("_eta_s"), f.get("_dwell_s")
    if eta is None:
        return PREDICT_CONFIRM
    window = PREDICT_CONFIRM * RENDER_INTERVAL_HINT
    if eta + (dwell or 0.0) <= window:
        return 1
    return PREDICT_CONFIRM


def _apply_confirmation(candidates):
    """Hold a predicted flight back until it has been predicted on
    PREDICT_CONFIRM consecutive renders, so one noisy projection never reaches
    the board on its own. Aircraft already in view bypass this entirely — a
    real sighting should never be delayed. Returns (kept, n_held)."""
    kept, seen = [], set()
    for f in candidates:
        key = f.get("hex") or f.get("callsign")
        seen.add(key)
        if f["in_view"]:
            _predict_streak[key] = PREDICT_CONFIRM    # real now; don't hold it
            kept.append(f)
            continue
        streak = _predict_streak.get(key, 0) + 1
        _predict_streak[key] = streak
        if streak >= _confirmations_for(f):
            kept.append(f)
    for key in list(_predict_streak):     # any gap in the scan breaks the streak
        if key not in seen:
            del _predict_streak[key]
    return kept, len(candidates) - len(kept)


# How much of the surroundings the map shows (radius in km from home). Much
# tighter than the prediction range so the Thames + local landmarks read; far
# inbound traffic is clamped to the map edge by the board.
MAP_RANGE_KM = float(os.environ.get("MAP_RANGE_KM", "20"))

# River Thames through central/east London, west→east, simplified (lat, lon).
# Plotted relative to home so the map shows the real course past the window.
_THAMES = [
    (51.4850, -0.1300), (51.4890, -0.1240), (51.4930, -0.1215),
    (51.4985, -0.1240), (51.5015, -0.1215), (51.5045, -0.1175),
    (51.5065, -0.1120), (51.5078, -0.1045), (51.5092, -0.0975),
    (51.5085, -0.0895), (51.5062, -0.0815), (51.5050, -0.0755),
    (51.5058, -0.0675), (51.5065, -0.0600), (51.5090, -0.0520),
    (51.5090, -0.0450), (51.5060, -0.0360), (51.5008, -0.0305),
    (51.4930, -0.0255), (51.4855, -0.0095), (51.4885,  0.0005),
    (51.4960, -0.0035), (51.5035,  0.0055), (51.5065,  0.0155),
    (51.5040,  0.0305), (51.5010,  0.0405),
]

# Notable landmarks south of the flat (lat, lon, short label).
_LANDMARKS = [
    (51.5045, -0.0865, "SHARD"),
    (51.5138, -0.0984, "ST PAUL'S"),
    (51.5054, -0.0235, "CANARY WHF"),
    (51.5030,  0.0032, "THE O2"),
    (51.5048,  0.0495, "CITY ARPT"),
    (51.4769, -0.0005, "GREENWICH"),
]


def _rel_km(lat, lon, home_lat, home_lon):
    """enu_km at metre precision for the map's JSON payload."""
    east, north = enu_km(lat, lon, home_lat, home_lon)
    return round(east, 3), round(north, 3)


def build_geo(home_lat, home_lon):
    """Thames + landmarks as east/north km offsets from home for the map."""
    return {
        "thames": [_rel_km(la, lo, home_lat, home_lon) for la, lo in _THAMES],
        "landmarks": [
            {"name": name, "e": (en := _rel_km(la, lo, home_lat, home_lon))[0], "n": en[1]}
            for la, lo, name in _LANDMARKS
        ],
    }


# Latest device telemetry, captured from /api/display request headers and fed
# into the next board render (one poll cycle of lag, which is fine for battery).
LAST_DEVICE = {"voltage": None, "percent": None, "rssi": None}


def battery_percent(v):
    """Rough LiPo state-of-charge from voltage (empty ~3.4 V, full ~4.15 V)."""
    if v is None:
        return None
    return max(0, min(100, round((v - 3.4) / (4.15 - 3.4) * 100)))


def record_device_state(batt, rssi):
    try:
        v = float(batt) if batt else None
    except (TypeError, ValueError):
        v = None
    if v is not None:
        LAST_DEVICE["voltage"] = round(v, 2)
        LAST_DEVICE["percent"] = battery_percent(v)
    try:
        LAST_DEVICE["rssi"] = int(float(rssi)) if rssi else LAST_DEVICE["rssi"]
    except (TypeError, ValueError):
        pass


def board_data(cfg):
    """In-view + soon-to-be-in-view aircraft for the display, each tagged with
    eta_min (0 = currently in view). Also returns the view-cone config so the
    board can draw the map."""
    ac_list = fetch_aircraft(cfg["source"], cfg["lat"], cfg["lon"], PREDICT_RANGE_KM)
    half = cfg["fov"] / 2.0
    out, n_turn = [], 0
    for ac in ac_list:
        lat, lon = ac["lat"], ac["lon"]
        alt_ft = ac.get("alt_ft")
        gs, track, vs = ac.get("gs_kt"), ac.get("track_deg"), ac.get("vs_fpm")
        dist = haversine_km(cfg["lat"], cfg["lon"], lat, lon)
        brg = bearing_deg(cfg["lat"], cfg["lon"], lat, lon)
        elev = elevation_deg(dist, alt_ft) if alt_ft is not None else None
        in_view = (dist <= cfg["range_km"]
                   and angle_diff(brg, cfg["bearing"]) <= half
                   and (elev is None or elev >= cfg["min_elev"]))
        omega = turn_rate_dps(ac.get("roll"), gs, ac.get("track_rate"))
        if omega is not None:
            n_turn += 1
        if in_view:
            eta_s, miss, dwell = 0.0, 0.0, None
        else:
            eta_s, miss, dwell = _entry_prediction(
                cfg, lat, lon, alt_ft, gs, track, vs, half, omega)
            if eta_s is not None and PREDICT_MISS_MAX_KM is not None \
                    and miss is not None and miss > PREDICT_MISS_MAX_KM:
                continue
        if eta_s is None:
            continue
        rec = _flight_record(ac, cfg, dist, brg, elev, in_view)
        rec["eta_min"] = round(eta_s / 60.0, 1)
        rec["miss_km"] = round(miss, 2) if miss is not None else None
        rec["turn_dps"] = round(omega, 2) if omega is not None else None
        rec["turning_for"] = approach_turn(lat, lon, alt_ft, track, omega, vs)
        rec["_eta_s"], rec["_dwell_s"] = eta_s, dwell   # for the confirm gate
        out.append(rec)
    out, n_held = _apply_confirmation(out)
    for f in out:
        f.pop("_eta_s", None); f.pop("_dwell_s", None)
    DIAG["n_turn"] = n_turn
    # in-view first, then soonest arrivals; keep the board readable
    out.sort(key=lambda f: (not f["in_view"], f["eta_min"], f["dist_km"]))
    out = out[:MAX_BOARD_FLIGHTS]
    enrich_routes(out)          # FR24 routes for just these (in-view first)
    fr24_usage()                # refresh credit counter for the debug panel
    return {
        "config": {
            "bearing": cfg["bearing"],
            "fov": cfg["fov"],
            "range_km": cfg["range_km"],
            "predict_range_km": PREDICT_RANGE_KM,
            "map_range_km": MAP_RANGE_KM,
            "horizon_min": PREDICT_HORIZON_S / 60.0,
            "geo": build_geo(cfg["lat"], cfg["lon"]),
            "battery": dict(LAST_DEVICE),
            "debug": {
                "source": DIAG["source"],
                "n_scan": DIAG["n_scan"],
                "n_board": len(out),
                "n_held": n_held,
                "n_enriched": DIAG["n_enriched"],
                "n_fr24": DIAG["n_fr24"],
                "n_turn": DIAG["n_turn"],
                "fetched_at": DIAG["fetched_at"],
                "error": DIAG["error"],
                "fr24": DIAG["fr24"],
                "credits_used": DIAG["credits_used"],
                "credits_est": DIAG["credits_est"],
                "credits_cap": DIAG["credits_cap"],
            },
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
REFRESH_RATE = int(os.environ.get("REFRESH_RATE", "30"))
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
            record_device_state(batt, rssi)
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
