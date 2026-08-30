FROM node:26-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# ------------------------------------------------------------
# System packages
# ------------------------------------------------------------

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        ffmpeg \
        ca-certificates \
        git \
    && rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------------
# Build bgutil PO-token HTTP provider
# ------------------------------------------------------------

RUN git clone \
        --depth 1 \
        --branch 1.3.1 \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
        /opt/bgutil-ytdlp-pot-provider \
    && cd /opt/bgutil-ytdlp-pot-provider/server \
    && npm ci --no-audit --no-fund \
    && npx tsc

# ------------------------------------------------------------
# Python dependencies
# ------------------------------------------------------------

COPY requirements.txt .

RUN pip3 install \
        --no-cache-dir \
        --break-system-packages \
        -r requirements.txt

# Install the bgutil yt-dlp plugin.
RUN pip3 install \
        --no-cache-dir \
        --break-system-packages \
        "bgutil-ytdlp-pot-provider==1.3.1"

# ------------------------------------------------------------
# Application
# ------------------------------------------------------------

COPY app.py .
COPY start.sh .

RUN chmod +x /app/start.sh \
    && mkdir -p /data/downloads

EXPOSE 10000

CMD ["/app/start.sh"]
