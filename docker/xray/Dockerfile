# Pin to a released upstream image; no downloading binaries at container runtime.
FROM ghcr.io/xtls/xray-core:26.3.27 AS upstream
FROM python:3.12-alpine
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=upstream /usr/local/bin/xray /usr/local/bin/xray
COPY --from=upstream /usr/local/share/xray/ /usr/local/share/xray/
COPY docker/xray/supervisor.py /opt/owrt-remote/supervisor.py
ENTRYPOINT ["python3", "-u", "/opt/owrt-remote/supervisor.py"]
