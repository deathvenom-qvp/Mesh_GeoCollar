# GeoCollar base station. Builds as a plain Docker image or as a Home Assistant app
# (Home Assistant passes BUILD_FROM from build.yaml).
ARG BUILD_FROM=python:3.12-slim
FROM ${BUILD_FROM}

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY geocollar_base ./geocollar_base

ENV GEOCOLLAR_DATA_DIR=/data \
    GEOCOLLAR_PORT=8099 \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8099
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/api/health', timeout=4)"
CMD ["python", "-m", "geocollar_base"]

LABEL org.opencontainers.image.title="GeoCollar base" \
      org.opencontainers.image.source="https://github.com/deathvenom-qvp/Mesh_GeoCollar" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"
