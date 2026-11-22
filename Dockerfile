# syntax=docker/dockerfile:1.7

FROM node:22-slim AS web-builder
ARG TTS_VERSION=0.2.0
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
ENV TTS_STUDIO_VERSION=${TTS_VERSION}
RUN npm run build

FROM ubuntu:24.04 AS runtime
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ARG TORCH=cu128
ARG INCLUDE_V5=1
ARG TTS_VERSION=0.2.0
ENV DEBIAN_FRONTEND=noninteractive \
    UV_NO_CACHE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    ffmpeg \
    git \
    libsndfile1 \
    sox \
    build-essential \
    cmake \
  && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY core/ /app/core/
COPY --from=web-builder /src/web/dist/ /app/web/dist/
COPY docker/entrypoint.sh /usr/local/bin/tts-studio-entrypoint.sh
RUN chmod +x /usr/local/bin/tts-studio-entrypoint.sh

# Initialize a minimal tagged git repo in /app/core so version.py, worker enrollment, and Colab bundling work cleanly
RUN cd /app/core && \
    git init -q -b master && \
    git config user.name "TTS Studio" && \
    git config user.email "noreply@tts-studio.local" && \
    git add -A && \
    git commit -q -m "TTS Studio ${TTS_VERSION}" && \
    git tag -a "v${TTS_VERSION#v}" -m "TTS Studio v${TTS_VERSION#v}"

RUN     ENGINES="v2pro,v2proplus"; \
    if [ "${INCLUDE_V5}" = "1" ]; then ENGINES="${ENGINES},v5turbo"; fi; \
    GSV_ROOT=/opt/GPT-SoVITS \
    GSV_V5_ROOT=/opt/GPT-SoVITS-v5 \
    TORCH="${TORCH}" \
    ENGINES="${ENGINES}" \
    PRETRAINED=skip \
    STEPS=venv,pth,nltk \
    SKIP_CHECK=1 \
      bash /app/core/scripts/install_linux.sh && \
    rm -rf /tmp/uv-cache

ENV TTSAPI_HOST=0.0.0.0 \
    TTSAPI_PORT=9880 \
    TTSAPI_INSTALL_PORT=0 \
    TTSAPI_DATA_DIR=/data \
    TTSAPI_LIBRARY=/library \
    TTSAPI_WEB_DIR=/app/web/dist \
    TTSAPI_GSV_ROOT=/opt/GPT-SoVITS \
    TTSAPI_GSV_V5_ROOT=/opt/GPT-SoVITS-v5 \
    TTSAPI_V5_PYTHON=/opt/GPT-SoVITS-v5/.venv/bin/python \
    TTSAPI_MODELS_DIR=/models \
    TTSAPI_ENGINES=v2pro,v2proplus \
    TTSAPI_AUTO_DOWNLOAD_MODELS=1

VOLUME ["/data", "/library", "/models"]
EXPOSE 9880

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${TTSAPI_PORT}/health || exit 1

ENTRYPOINT ["/usr/local/bin/tts-studio-entrypoint.sh"]
