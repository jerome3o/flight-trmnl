#!/usr/bin/env python3
"""
window_flights.py — What planes can I see out my window right now?

Queries a free, no-API-key ADS-B aggregator (adsb.lol by default, with
airplanes.live and adsb.fi as alternatives) and filters aircraft to a
"view cone": a circle segment defined by your position, the compass
bearing your window faces, and a horizontal field-of-view.

Usage examples:

    # Window faces due south (180°), 90° wide view, out to 40 km
    python window_flights.py --lat 51.5074 --lon -0.1278 \
        --bearing 180 --fov 90 --range-km 40

    # Watch mode: refresh every 15 s
    python window_flights.py --lat 51.5074 --lon -0.1278 \
        --bearing 240 --fov 70 --range-km 30 --watch 15

    # Use a different data source
    python window_flights.py ... --source airplaneslive

Only needs the standard library + `requests`:
    pip install requests
"""

import argparse
import math
import sys
import time
from datetime import datetime

import requests

# ----------------------------------------------------------------------
# Data sources (all free, no key required, same JSON schema: "ac" list)
# ----------------------------------------------------------------------
SOURCES = {
    "adsblol":       "https://api.adsb.lol/v2/point/{lat}/{lon}/{nm}",
    "airplaneslive": "https://api.airplanes.live/v2/point/{lat}/{lon}/{nm}",
    "adsbfi":        "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{nm}",
}

EARTH_R_KM = 6371.0088


# ----------------------------------------------------------------------
# Geometry helpers
# ----------------------------------------------------------------------
def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    """Initial compass bearing from point 1 to point 2, 0–360°."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def angle_diff(a, b):
    """Smallest absolute difference between two bearings, 0–180°."""
    d = abs(a - b) % 360.0
    return d if d <= 180.0 else 360.0 - d


def elevation_deg(distance_km, alt_ft, observer_alt_ft=0.0):
    """Approximate elevation angle above the horizon (ignores refraction)."""
    alt_km = (alt_ft - observer_alt_ft) * 0.0003048
    if distance_km < 0.01:
        return 90.0
    # Account for Earth curvature dropping the target below the horizontal
    curvature_drop_km = distance_km ** 2 / (2 * EARTH_R_KM)
    return math.degrees(math.atan2(alt_km - curvature_drop_km, distance_km))


def cardinal(deg):
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[int((deg + 11.25) // 22.5) % 16]


# ----------------------------------------------------------------------
# Fetch + filter
# ----------------------------------------------------------------------
def fetch_aircraft(source, lat, lon, radius_km):
    radius_nm = min(radius_km / 1.852, 250)  # APIs cap around 250 nm
    url = SOURCES[source].format(lat=lat, lon=lon, nm=round(radius_nm, 1))
    resp = requests.get(url, timeout=15, headers={"User-Agent": "window-flights/1.0"})
    resp.raise_for_status()
    return resp.json().get("ac", []) or []


def get_alt_ft(ac):
    """Altitude in feet; 'ground' means on the ground."""
    alt = ac.get("alt_geom", ac.get("alt_baro"))
    if alt in (None, "ground"):
        return None
    try:
        return float(alt)
    except (TypeError, ValueError):
        return None


def visible_flights(aircraft, lat, lon, view_bearing, fov, max_km,
                    min_elev, observer_alt_ft, include_ground):
    half_fov = fov / 2.0
    out = []
    for ac in aircraft:
        ac_lat, ac_lon = ac.get("lat"), ac.get("lon")
        if ac_lat is None or ac_lon is None:
            continue

        alt_ft = get_alt_ft(ac)
        if alt_ft is None and not include_ground:
            continue

        dist = haversine_km(lat, lon, ac_lat, ac_lon)
        if dist > max_km:
            continue

        brg = bearing_deg(lat, lon, ac_lat, ac_lon)
        if angle_diff(brg, view_bearing) > half_fov:
            continue

        elev = elevation_deg(dist, alt_ft or 0.0, observer_alt_ft)
        if elev < min_elev:
            continue

        out.append({
            "callsign": (ac.get("flight") or "").strip() or ac.get("r", "?"),
            "hex": ac.get("hex", "?"),
            "type": ac.get("t", "?"),
            "dist_km": dist,
            "bearing": brg,
            "elev": elev,
            "alt_ft": alt_ft,
            "gs_kt": ac.get("gs"),
            "track": ac.get("track"),
        })
    out.sort(key=lambda f: f["dist_km"])
    return out


# ----------------------------------------------------------------------
# Display
# ----------------------------------------------------------------------
def print_report(flights, args):
    now = datetime.now().strftime("%H:%M:%S")
    lo = (args.bearing - args.fov / 2) % 360
    hi = (args.bearing + args.fov / 2) % 360
    print(f"\n[{now}]  View: {cardinal(args.bearing)} ({args.bearing:.0f}°), "
          f"cone {lo:.0f}°–{hi:.0f}°, range {args.range_km:.0f} km")

    if not flights:
        print("  No aircraft in view right now.")
        return

    hdr = f"  {'CALLSIGN':<9} {'TYPE':<5} {'DIST':>7} {'DIR':>9} {'ELEV':>6} {'ALT':>9} {'SPD':>6} {'HDG':>5}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for f in flights:
        alt = f"{f['alt_ft']:,.0f}ft" if f["alt_ft"] is not None else "ground"
        spd = f"{f['gs_kt']:.0f}kt" if f.get("gs_kt") is not None else "-"
        trk = f"{f['track']:.0f}°" if f.get("track") is not None else "-"
        direction = f"{f['bearing']:.0f}° {cardinal(f['bearing'])}"
        print(f"  {f['callsign']:<9} {f['type']:<5} {f['dist_km']:>5.1f}km "
              f"{direction:>9} {f['elev']:>5.1f}° {alt:>9} {spd:>6} {trk:>5}")


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="Show flights visible from your window.")
    p.add_argument("--lat", type=float, required=True, help="Your latitude")
    p.add_argument("--lon", type=float, required=True, help="Your longitude")
    p.add_argument("--bearing", type=float, required=True,
                   help="Compass direction your window faces (0=N, 90=E, 180=S, 270=W)")
    p.add_argument("--fov", type=float, default=90,
                   help="Horizontal field of view in degrees (default 90)")
    p.add_argument("--range-km", type=float, default=40,
                   help="Maximum distance in km (default 40)")
    p.add_argument("--min-elev", type=float, default=0,
                   help="Minimum elevation angle above horizon in degrees "
                        "(e.g. 5 if buildings block the horizon; default 0)")
    p.add_argument("--observer-alt-ft", type=float, default=0,
                   help="Your altitude above sea level in feet (default 0)")
    p.add_argument("--include-ground", action="store_true",
                   help="Include aircraft on the ground")
    p.add_argument("--source", choices=SOURCES, default="adsblol",
                   help="ADS-B data source (default adsblol)")
    p.add_argument("--watch", type=float, metavar="SECONDS",
                   help="Refresh continuously every N seconds")
    args = p.parse_args()

    def run_once():
        try:
            ac = fetch_aircraft(args.source, args.lat, args.lon, args.range_km)
        except requests.RequestException as e:
            print(f"  Error fetching data from {args.source}: {e}", file=sys.stderr)
            return
        flights = visible_flights(ac, args.lat, args.lon, args.bearing, args.fov,
                                  args.range_km, args.min_elev,
                                  args.observer_alt_ft, args.include_ground)
        print_report(flights, args)

    if args.watch:
        try:
            while True:
                run_once()
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        run_once()


if __name__ == "__main__":
    main()
