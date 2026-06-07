FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium \
        fluxbox \
        novnc \
        procps \
        websockify \
        x11vnc \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        selenium \
        undetected-chromedriver

WORKDIR /app
COPY . /app

RUN chmod +x /app/scripts/start_linux_vnc.sh

EXPOSE 3030 5901 6080

CMD ["/app/scripts/start_linux_vnc.sh"]
