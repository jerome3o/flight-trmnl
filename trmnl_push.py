#!/usr/bin/env python3
"""
trmnl_push.py — push the window's visible aircraft to a TRMNL device.

Uses the TRMNL private-plugin *webhook* strategy: outbound POSTs of
merge variables to usetrmnl.com. Nothing needs to be exposed to the
internet. The plugin's Liquid template (the UI, done later) renders
whatever arrives here.

Config (environment variables):

  TRMNL_PLUGIN_UUID   Plugin Setting UUID from the private plugin page
                      (or set TRMNL_WEBHOOK_URL with the full URL)
  PUSH_INTERVAL       Seconds between pushes. Default 300. Floored at
                      300 (12/hr standard plan) unless TRMNL_PLUS=1,
                      which floors at 120 (30/hr).
  MAX_AIRCRAFT        Max planes per payload (default 5)
  PAYLOAD_LIMIT       Byte budget for the JSON body (default 1900,
                      under TRMNL's 2 KB cap; TRMNL+ may raise to ~4900)

  LAT LON BEARING FOV RANGE_KM MIN_ELEV SOURCE
                      Override the view cone (shared with serve.py)

Merge-variable contract (what the Liquid template will see):

  updated_at    "18:42" (UTC HH:MM)
  in_view       total aircraft in the cone (may exceed len(planes))
  planes        list, nearest first, each:
     airline    "British Airways" or null
     callsign   "BAW172"
     type       "B772"
     type_name  "Boeing 777-200" or null
     origin     "LHR" (IATA, falls back to ICAO) or null
     origin_city "London" or null
     country    "NL" (ISO 3166 country of registry) or null
     dist_km    3.0          alt_ft    6000 or null
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import serve  # local module: DEFAULTS (env-aware) + visible_aircraft()

WEBHOOK_URL = os.environ.get("TRMNL_WEBHOOK_URL") or (
    "https://usetrmnl.com/api/custom_plugins/" + os.environ["TRMNL_PLUGIN_UUID"]
    if os.environ.get("TRMNL_PLUGIN_UUID") else None
)
IS_PLUS = os.environ.get("TRMNL_PLUS") == "1"
FLOOR = 120 if IS_PLUS else 300
PUSH_INTERVAL = max(int(os.environ.get("PUSH_INTERVAL", "300")), FLOOR)
MAX_AIRCRAFT = int(os.environ.get("MAX_AIRCRAFT", "5"))
PAYLOAD_LIMIT = int(os.environ.get("PAYLOAD_LIMIT", "1900"))

CARD = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
        "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def cardinal(deg):
    return CARD[round(deg / 22.5) % 16]


def compact(f):
    """One aircraft, trimmed to e-ink essentials."""
    origin = f.get("origin") or {}
    return {
        "airline": (f.get("airline") or "")[:26] or None,
        "callsign": f.get("callsign"),
        "type": f.get("type"),
        "type_name": (f.get("type_desc") or "")[:26] or None,
        "origin": origin.get("iata") or origin.get("icao"),
        "country": f.get("country"),
        "origin_city": (origin.get("city") or "")[:18] or None,
        "dist_km": f.get("dist_km"),
        "alt_ft": int(f["alt_ft"]) if f.get("alt_ft") is not None else None,
    }


def build_payload(aircraft):
    """Assemble merge variables, dropping farthest planes until the
    serialized body fits the byte budget."""
    planes = [compact(f) for f in aircraft[:MAX_AIRCRAFT]]
    while True:
        body = {
            "merge_variables": {
                "updated_at": datetime.now(timezone.utc).strftime("%H:%M"),
                "in_view": len(aircraft),
                "planes": planes,
            }
        }
        raw = json.dumps(body, separators=(",", ":")).encode()
        if len(raw) <= PAYLOAD_LIMIT or not planes:
            return body, raw
        planes.pop()  # farthest goes first


def push(raw):
    req = urllib.request.Request(
        WEBHOOK_URL, data=raw,
        headers={"Content-Type": "application/json",
                 "User-Agent": "window-flights/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.status, resp.read().decode(errors="replace")


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    if not WEBHOOK_URL:
        log("TRMNL_PLUGIN_UUID / TRMNL_WEBHOOK_URL not set — pusher disabled.")
        sys.exit(0)
    log(f"Pushing to TRMNL every {PUSH_INTERVAL}s "
        f"({'TRMNL+' if IS_PLUS else 'standard'} rate limits), "
        f"cone: {serve.DEFAULTS['bearing']:.0f}°/{serve.DEFAULTS['fov']:.0f}° "
        f"{serve.DEFAULTS['range_km']:.0f}km from "
        f"{serve.DEFAULTS['lat']:.4f},{serve.DEFAULTS['lon']:.4f}")
    backoff = 0
    while True:
        try:
            aircraft = serve.visible_aircraft(dict(serve.DEFAULTS))
            body, raw = build_payload(aircraft)
            status, text = push(raw)
            n = len(body["merge_variables"]["planes"])
            log(f"pushed {n}/{len(aircraft)} planes, {len(raw)} bytes -> HTTP {status}")
            backoff = 0
        except urllib.error.HTTPError as e:
            if e.code == 429:
                backoff = min((backoff or PUSH_INTERVAL) * 2, 3600)
                log(f"429 rate limited — backing off {backoff}s")
            else:
                log(f"TRMNL error HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
        except Exception as e:
            log(f"push cycle failed: {e}")
        time.sleep(backoff or PUSH_INTERVAL)


if __name__ == "__main__":
    main()
