#!/usr/bin/env python3
"""
route_probe.py — which route providers actually work right now?

Tests every provider (adsbdb, hexdb, adsb.lol routeset) against real
callsigns and prints a verdict table. Run it from the folder containing
serve.py:

    python route_probe.py                # auto-grab live callsigns overhead
    python route_probe.py BAW172 EZY45KG # or test specific callsigns

Ends with a recommended ROUTE_PROVIDERS ordering you can export.
"""

import json
import sys
import time

import serve


def live_callsigns(n=6):
    """Grab airline-style callsigns currently within 40 km of the window."""
    cfg = dict(serve.DEFAULTS)
    cfg["range_km"] = 40
    ac = serve.fetch_upstream(cfg["source"], cfg["lat"], cfg["lon"], cfg["range_km"])
    seen, out = set(), []
    for a in ac:
        cs = (a.get("flight") or "").strip()
        if cs and cs not in seen and serve.AIRLINE_CS.match(cs):
            seen.add(cs)
            out.append((cs, a.get("lat") or cfg["lat"], a.get("lon") or cfg["lon"]))
        if len(out) >= n:
            break
    return out


def probe(callsigns):
    stats = {name: {"ok": 0, "miss": 0, "err": 0, "ms": []}
             for name in serve.PROVIDERS}
    width = max(len(c) for c, _, _ in callsigns) + 2

    for cs, lat, lon in callsigns:
        print(f"\n{cs}")
        for name, fn in serve.PROVIDERS.items():
            t0 = time.time()
            try:
                result = fn(cs, lat, lon)
                ms = int((time.time() - t0) * 1000)
                stats[name]["ms"].append(ms)
                if result:
                    o, d = result
                    stats[name]["ok"] += 1
                    print(f"  {name:<8} OK    {o.get('iata') or o.get('icao'):>4} "
                          f"({o.get('city') or o.get('name') or '?'}) -> "
                          f"{d.get('iata') or d.get('icao')} "
                          f"[{o.get('country')}] {ms}ms")
                else:
                    stats[name]["miss"] += 1
                    print(f"  {name:<8} miss  (not in this database) {ms}ms")
            except Exception as e:
                stats[name]["err"] += 1
                print(f"  {name:<8} ERROR {type(e).__name__}: {str(e)[:120]}")

    print("\n" + "=" * 60)
    print(f"{'provider':<10}{'ok':>5}{'miss':>6}{'error':>7}{'median ms':>11}")
    ranked = []
    for name, s in stats.items():
        med = sorted(s["ms"])[len(s["ms"]) // 2] if s["ms"] else "-"
        print(f"{name:<10}{s['ok']:>5}{s['miss']:>6}{s['err']:>7}{med:>11}")
        ranked.append((-(s["ok"]), s["err"], name))
    order = ",".join(name for _, _, name in sorted(ranked))
    print("=" * 60)
    print(f"\nSuggested order (most hits first, errors demoted):")
    print(f"  export ROUTE_PROVIDERS={order}")
    print("Providers that only ERROR are broken/unreachable; drop them from the list.")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cs_list = [(cs.upper(), serve.DEFAULTS["lat"], serve.DEFAULTS["lon"])
                   for cs in sys.argv[1:]]
    else:
        print("Grabbing live callsigns within 40 km…")
        cs_list = live_callsigns()
        if not cs_list:
            print("Quiet sky — pass callsigns explicitly: "
                  "python route_probe.py BAW172 KLM1001")
            sys.exit(1)
        print("Testing:", ", ".join(c for c, _, _ in cs_list))
    probe(cs_list)
