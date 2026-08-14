#!/bin/sh
# window-flights container: data API + webapp + BYOS device server + renderer.
#
# Every process started here is load-bearing, but only serve.py used to be
# waited on. That made a dead renderer invisible in the worst possible way:
# the container stayed healthy, the deploy went green, and the panel kept
# showing its last image indefinitely — /api/display hands the device a
# content-hash filename, and an unchanging image means the device never
# redraws. A frozen board and a healthy one look identical from outside.
#
# So supervise all of them. Each child reports its exit through a FIFO, and
# the first one to go takes the container down, where the platform's restart
# policy can see the failure instead of it being swallowed.
set -u

EXITS=$(mktemp -u /tmp/exits.XXXXXX)
mkfifo "$EXITS" || exit 1
# Held open read-write for the life of the script so the pipe never reaches
# EOF between children exiting, and so a child's write never blocks waiting
# for a reader to show up.
exec 3<> "$EXITS"

start() {                      # start <name> <command...>
  name=$1; shift
  ( "$@"; echo "$name $?" > "$EXITS" ) &
}

start serve  python -u serve.py --host 0.0.0.0 --port "${PORT:-8000}"
start render python -u render_loop.py

# optional TRMNL-cloud pusher (only if a plugin UUID is configured)
if [ -n "${TRMNL_WEBHOOK_URL:-}" ] || [ -n "${TRMNL_PLUGIN_UUID:-}" ]; then
  start push python -u trmnl_push.py
fi

# Shut down promptly on `docker stop` / `fly apps restart` rather than sitting
# out the grace period and being killed.
trap 'trap - INT TERM; echo "[entrypoint] stopping" >&2; kill 0 2>/dev/null; exit 0' INT TERM

while read -r who code <&3; do
  # The renderer bows out with status 0 when chromium or ImageMagick are
  # missing. That is a supported way to run this image — the API and web board
  # work fine without BYOS rendering — so it is not a reason to stop.
  if [ "$who" = render ] && [ "$code" -eq 0 ]; then
    echo "[entrypoint] renderer disabled itself; continuing without BYOS rendering" >&2
    continue
  fi
  echo "[entrypoint] $who exited with status $code — stopping the container" >&2
  break
done

# Clear the trap first: with it armed, the group signal below would re-enter
# it and exit 0, reporting a clean shutdown for what is a failure.
trap - INT TERM
rm -f "$EXITS"
kill 0 2>/dev/null              # take the surviving children down too
exit 1
