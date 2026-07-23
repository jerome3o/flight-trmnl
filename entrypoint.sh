#!/bin/sh
# window-flights container: data API + webapp + BYOS device server + renderer.
python -u serve.py --host 0.0.0.0 --port "${PORT:-8000}" &
SERVER=$!

# BYOS renderer (self-disables with a log line if chromium/magick missing)
python -u render_loop.py &

# optional TRMNL-cloud pusher (only if a plugin UUID is configured)
if [ -n "$TRMNL_WEBHOOK_URL" ] || [ -n "$TRMNL_PLUGIN_UUID" ]; then
  python -u trmnl_push.py &
fi

wait $SERVER
