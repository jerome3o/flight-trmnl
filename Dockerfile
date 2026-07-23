FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium imagemagick fonts-dejavu-core fonts-liberation tzdata \
    && rm -rf /var/lib/apt/lists/*

# Render the board clock in local (London) time; Chromium honours $TZ.
ENV TZ=Europe/London

WORKDIR /app
COPY serve.py trmnl_push.py render_loop.py route_probe.py \
     window-flights.html trmnl-board.html entrypoint.sh ./
RUN chmod +x entrypoint.sh

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["./entrypoint.sh"]
