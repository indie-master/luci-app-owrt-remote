FROM python:3.12-slim-bookworm AS runtime
ARG XRAY_VERSION=26.3.27
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates unzip curl && rm -rf /var/lib/apt/lists/*
RUN set -eux; case "${TARGETARCH:-amd64}" in amd64) arch=64;; arm64) arch=arm64-v8a;; *) exit 1;; esac; \
    curl -fsSL "https://github.com/XTLS/Xray-core/releases/download/v${XRAY_VERSION}/Xray-linux-${arch}.zip" -o /tmp/xray.zip; \
    unzip -p /tmp/xray.zip xray > /usr/local/bin/xray; chmod 755 /usr/local/bin/xray; rm /tmp/xray.zip
COPY docker/xray/supervisor.py /supervisor.py
ENTRYPOINT ["python3", "/supervisor.py"]
