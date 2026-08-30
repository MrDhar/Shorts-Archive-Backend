FROM node:26-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip ffmpeg ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# Build the bgutil PO-token provider in this same container.
RUN git clone --depth 1 --branch 1.3.1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil-ytdlp-pot-provider \
    && cd /opt/bgutil-ytdlp-pot-provider/server \
    && npm ci --omit=dev --no-audit --no-fund \
    && npm ci --no-audit --no-fund \
    && npx tsc

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY app.py .
COPY start.sh .
RUN chmod +x /app/start.sh && mkdir -p /data/downloads

# Render supplies PORT for the public HTTP service. The POT provider stays private on localhost:4416.
EXPOSE 10000 4416

CMD ["/app/start.sh"]
