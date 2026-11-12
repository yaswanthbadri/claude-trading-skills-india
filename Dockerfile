# The always-on market desk.
#
# One container runs tools/scheduler.py, which fires every job on an IST clock
# and supervises the Telegram listener. No cron, no launchd, no laptop.
#
#   docker compose up -d --build      # start
#   docker compose logs -f            # watch it work
#   docker compose exec desk python tools/subscribers.py   # who gets reports
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Kolkata \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code only. Market data, reports, logs and secrets are mounted at runtime so a
# rebuild never wipes the cache or the scorecard.
COPY tools ./tools
COPY skills ./skills
COPY data/universe.csv ./data/universe.csv

RUN mkdir -p data/candles reports logs

CMD ["python", "tools/scheduler.py"]
