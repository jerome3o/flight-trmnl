#!/usr/bin/env python3
"""
render_loop.py — keeps latest.png fresh for the BYOS display endpoint.

Loop: headless Chrome screenshots the split-flap board at the panel's native
resolution, ImageMagick quantizes it to a TRMNL greyscale palette (per
TRMNL's own ImageMagick guide), and the result is atomically swapped into
place. serve.py's /api/display hands the device a content-hash filename, so a
render identical to the last one costs the e-ink nothing.

Chrome counts browser chrome inside --window-size, so the viewport the board
lays itself out against is shorter than the panel and the render stops short,
leaving a blank strip along the bottom (87px at 1872x1404). The shortfall is
measured once at startup and added to the requested height, then the
screenshot is cropped back to the panel's exact size — which the firmware
requires and will reject the image without.

Match RENDER_WIDTH/HEIGHT/DEPTH to your device — the firmware rejects any
image that isn't the panel's exact size and does not scale:
  · TRMNL X  (10.3"): 1872x1404, 4bit  (16-level greyscale PNG)  ← default
  · TRMNL OG (7.5") :  800x480,  2bit  (4-level greyscale) or 1bit (legacy)

Environment:
  BOARD_URL        default http://127.0.0.1:8000/trmnl-board.html
  RENDER_OUT       default latest.png (must match serve.py's RENDER_OUT)
  RENDER_INTERVAL  seconds between renders, default 55
  RENDER_WIDTH     panel width in px, default 1872
  RENDER_HEIGHT    panel height in px, default 1404
  RENDER_DEPTH     "4bit" (default, TRMNL X) | "2bit" | "1bit" (legacy)
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

BOARD_URL = os.environ.get("BOARD_URL", "http://127.0.0.1:8000/trmnl-board.html")
OUT = os.environ.get("RENDER_OUT", "latest.png")
INTERVAL = int(os.environ.get("RENDER_INTERVAL", "55"))
WIDTH = int(os.environ.get("RENDER_WIDTH", "1872"))
HEIGHT = int(os.environ.get("RENDER_HEIGHT", "1404"))
DEPTH = os.environ.get("RENDER_DEPTH", "4bit")

# Firmware image-size ceiling: ~90 KB on OG boards, ~750 KB on X-class PSRAM.
MAX_IMAGE_BYTES = int(os.environ.get("RENDER_MAX_BYTES", "750000"))

CHROME_CANDIDATES = ["chromium", "chromium-browser", "google-chrome",
                     "google-chrome-stable", "chrome"]
CHROME_FLAGS = ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                "--hide-scrollbars",
                "--force-device-scale-factor=1", "--virtual-time-budget=4000"]

# Headless Chrome counts browser chrome inside --window-size, so the viewport
# it hands the page is shorter than what we asked for — the board lays itself
# out against 100vh and stops short of the panel, leaving a blank strip along
# the bottom (87px at 1872x1404 on Debian's chromium). Measured once at
# startup and added back to the requested height; publish() then crops the
# screenshot to the panel's exact size, so the firmware still gets what it
# demands however Chrome behaves.
WINDOW_PAD = 0


def measure_window_pad():
    """How many pixels shorter than --window-size the viewport comes out."""
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as fh:
        fh.write("<html><body><script>"
                 "document.title='VP'+window.innerHeight"
                 "</script></body></html>")
        probe = fh.name
    try:
        r = subprocess.run([CHROME, "--headless=new", *CHROME_FLAGS,
                            f"--window-size={WIDTH},{HEIGHT}", "--dump-dom",
                            "file://" + probe],
                           capture_output=True, timeout=60)
        m = re.search(rb"VP(\d+)", r.stdout)
        if m:
            pad = HEIGHT - int(m.group(1))
            # sanity-bound it: a wild reading means the probe misfired
            return pad if 0 <= pad < HEIGHT // 3 else 0
    except Exception as e:
        log(f"viewport probe failed ({e}); rendering without compensation")
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass
    return 0


def log(msg):
    print(f"[render] {msg}", flush=True)


def find(candidates):
    for c in candidates:
        if shutil.which(c):
            return c
    return None


CHROME = find(CHROME_CANDIDATES)
MAGICK = find(["magick"]) or find(["convert"])


# Force the panel's exact pixel size: crop off the compensation padding, and
# pad with white if a render ever comes back short. The firmware rejects any
# image that isn't the panel's exact size, so this is the guarantee.
FIT = ["-crop", f"{WIDTH}x{HEIGHT}+0+0", "+repage",
       "-background", "white", "-extent", f"{WIDTH}x{HEIGHT}"]


def quantize(src, dst):
    """Reduce the screenshot to the TRMNL greyscale depth the panel expects.
    4-bit: 16 greys (TRMNL X); 2-bit: 4 greys; 1-bit: black/white (legacy).
    Recipes follow TRMNL's ImageMagick guide (posterize + -depth per mode)."""
    if DEPTH == "1bit":
        cmd = [MAGICK, src, *FIT, "-dither", "FloydSteinberg",
               "-remap", "pattern:gray50", "-depth", "1", "-strip", f"png:{dst}"]
    elif DEPTH == "4bit":
        cmd = [MAGICK, src, *FIT, "-colorspace", "Gray", "-dither", "FloydSteinberg",
               "-posterize", "16", "-alpha", "off", "-depth", "4",
               "-strip", f"png:{dst}"]
    else:  # 2bit
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as cm:
            cmap = cm.name
        subprocess.run([MAGICK, "-size", "4x1",
                        "xc:#000000", "xc:#555555", "xc:#aaaaaa", "xc:#ffffff",
                        "+append", "-type", "Palette", cmap], check=True)
        cmd = [MAGICK, src, *FIT, "-dither", "FloydSteinberg", "-remap", cmap,
               "-define", "png:bit-depth=2", "-define", "png:color-type=0",
               "-strip", f"png:{dst}"]
    subprocess.run(cmd, check=True, capture_output=True)


def screenshot(dst):
    for headless in ("--headless=new", "--headless"):
        # WINDOW_PAD was measured against --headless=new; the legacy mode has
        # its own (usually zero) shortfall, so don't apply it there.
        pad = WINDOW_PAD if headless == "--headless=new" else 0
        cmd = [CHROME, headless, *CHROME_FLAGS,
               f"--window-size={WIDTH},{HEIGHT + pad}",
               f"--screenshot={dst}", BOARD_URL]
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
    size = os.path.getsize(final_tmp)
    if size > MAX_IMAGE_BYTES:
        log(f"WARNING: image is {size} bytes (> {MAX_IMAGE_BYTES}); the device "
            f"may reject it as too big. Consider a lower RENDER_DEPTH.")
    os.replace(final_tmp, OUT)


def placeholder():
    """A valid screen before the first render, so a freshly-docked device
    shows something sane instead of a 404."""
    if os.path.exists(OUT) or not MAGICK:
        return
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as t:
        tmp = t.name
    subprocess.run([MAGICK, "-size", f"{WIDTH}x{HEIGHT}", "xc:#ffffff",
                    "-fill", "#000000", "-gravity", "center",
                    "-pointsize", str(max(24, HEIGHT // 14)), "-annotate", "0",
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
    global WINDOW_PAD
    if not CHROME:
        log("no Chrome/Chromium found — renderer disabled "
            "(install chromium, or run outside docker without BYOS rendering)")
        sys.exit(0)
    if not MAGICK:
        log("ImageMagick not found — renderer disabled")
        sys.exit(0)
    WINDOW_PAD = measure_window_pad()
    if WINDOW_PAD:
        log(f"viewport runs {WINDOW_PAD}px short of --window-size; "
            f"requesting {HEIGHT + WINDOW_PAD}px so the board fills the panel")
    log(f"chrome={CHROME} magick={MAGICK} {WIDTH}x{HEIGHT} depth={DEPTH} "
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
