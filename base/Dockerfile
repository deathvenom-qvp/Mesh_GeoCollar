# GeoCollar base station. Builds as a plain Docker image or as a Home Assistant app
# (Home Assistant passes BUILD_FROM from build.yaml).
ARG BUILD_FROM=python:3.12-slim
FROM ${BUILD_FROM}

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY geocollar_base ./geocollar_base

ENV GEOCOLLAR_DATA_DIR=/data \
    GEOCOLLAR_PORT=8099
VOLUME ["/data"]
EXPOSE 8099
CMD ["python", "-m", "geocollar_base"]
