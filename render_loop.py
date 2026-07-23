#!/usr/bin/env python3
"""
render_loop.py — keeps latest.png fresh for the BYOS display endpoint.

Loop: headless Chrome screenshots the split-flap board at 800x480,
ImageMagick quantizes it to the TRMNL 2-bit greyscale palette
(#000/#555/#aaa/#fff, per TRMNL's own ImageMagick guide), and the result
is atomically swapped into place. serve.py's /api/display hands the
device a content-hash filename, so a render identical to the last one
costs the e-ink nothing.

Environment:
  BOARD_URL        default http://127.0.0.1:8000/trmnl-board.html
  RENDER_OUT       default latest.png (must match serve.py's RENDER_OUT)
  RENDER_INTERVAL  seconds between renders, default 55
  RENDER_DEPTH     "2bit" (default, FW 1.6.0+ greyscale) or "1bit" (legacy)
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

BOARD_URL = os.environ.get("BOARD_URL", "http://127.0.0.1:8000/trmnl-board.html")
OUT = os.environ.get("RENDER_OUT", "latest.png")
INTERVAL = int(os.environ.get("RENDER_INTERVAL", "55"))
DEPTH = os.environ.get("RENDER_DEPTH", "2bit")

CHROME_CANDIDATES = ["chromium", "chromium-browser", "google-chrome",
                     "google-chrome-stable", "chrome"]
CHROME_FLAGS = ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                "--hide-scrollbars", "--window-size=800,480",
                "--virtual-time-budget=4000"]


def log(msg):
    print(f"[render] {msg}", flush=True)


def find(candidates):
    for c in candidates:
        if shutil.which(c):
            return c
    return None


CHROME = find(CHROME_CANDIDATES)
MAGICK = find(["magick"]) or find(["convert"])


def quantize(src, dst):
    """Map to the exact TRMNL palette. 2-bit: 4 greys; 1-bit: black/white."""
    if DEPTH == "1bit":
        cmd = [MAGICK, src, "-dither", "FloydSteinberg",
               "-remap", "pattern:gray50", "-depth", "1", "-strip", f"png:{dst}"]
    else:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as cm:
            cmap = cm.name
        subprocess.run([MAGICK, "-size", "4x1",
                        "xc:#000000", "xc:#555555", "xc:#aaaaaa", "xc:#ffffff",
                        "+append", "-type", "Palette", cmap], check=True)
        cmd = [MAGICK, src, "-dither", "FloydSteinberg", "-remap", cmap,
               "-define", "png:bit-depth=2", "-define", "png:color-type=0",
               "-strip", f"png:{dst}"]
    subprocess.run(cmd, check=True, capture_output=True)


def screenshot(dst):
    for headless in ("--headless=new", "--headless"):
        cmd = [CHROME, headless, *CHROME_FLAGS, f"--screenshot={dst}", BOARD_URL]
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        if r.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
            return True
        log(f"chrome ({headless}) failed rc={r.returncode}: "
            f"{r.stderr.decode(errors='replace')[-200:]}")
    return False


def publish(tmp):
    """Atomic swap so /api/display never serves a half-written file."""
    final_tmp = OUT + ".tmp"
    quantize(tmp, final_tmp)
    os.replace(final_tmp, OUT)


def placeholder():
    """A valid screen before the first render, so a freshly-docked device
    shows something sane instead of a 404."""
    if os.path.exists(OUT) or not MAGICK:
        return
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as t:
        tmp = t.name
    subprocess.run([MAGICK, "-size", "800x480", "xc:#000000",
                    "-fill", "#ffffff", "-gravity", "center",
                    "-pointsize", "34", "-annotate", "0",
                    "OVERHEAD\nwaiting for first render…", tmp],
                   check=False, capture_output=True)
    try:
        publish(tmp)
        log("placeholder published")
    except Exception as e:
        log(f"placeholder failed: {e}")


def wait_for_server(tries=30):
    for _ in range(tries):
        try:
            urllib.request.urlopen(BOARD_URL, timeout=3)
            return True
        except Exception:
            time.sleep(1)
    return False


def main():
    if not CHROME:
        log("no Chrome/Chromium found — renderer disabled "
            "(install chromium, or run outside docker without BYOS rendering)")
        sys.exit(0)
    if not MAGICK:
        log("ImageMagick not found — renderer disabled")
        sys.exit(0)
    log(f"chrome={CHROME} magick={MAGICK} depth={DEPTH} "
        f"every {INTERVAL}s -> {OUT}")
    placeholder()
    if not wait_for_server():
        log(f"board never became reachable at {BOARD_URL}")
        sys.exit(1)
    while True:
        t0 = time.time()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as t:
            tmp = t.name
        try:
            if screenshot(tmp):
                publish(tmp)
                log(f"rendered in {time.time()-t0:.1f}s")
        except Exception as e:
            log(f"render cycle failed: {e}")
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        time.sleep(max(1, INTERVAL - (time.time() - t0)))


if __name__ == "__main__":
    main()
