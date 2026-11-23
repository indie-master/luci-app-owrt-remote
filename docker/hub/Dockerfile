# Reviewed runtime only. Host Xray and Nginx stay outside this image.
# SHA is supplied by docker/manager.py; a mismatched source fails the build.
FROM python:3.10-slim@sha256:6ff506466f8b1e981719b468cc0023b8ef000b983cdec0eafe119d01f379e31d
ARG HUB_SOURCE_SHA256
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    OWRT_REMOTE_STATE_DIR=/var/lib/owrt-remote
WORKDIR /opt/owrt-remote
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      openssh-client ca-certificates procps iproute2 curl \
    && rm -rf /var/lib/apt/lists/*
COPY docker/hub/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock
COPY vps/owrt-remote-hub.py /opt/owrt-remote/owrt-remote-hub.py
COPY docker/transport_profiles.py /opt/owrt-remote/transport_profiles.py
RUN test -n "$HUB_SOURCE_SHA256" && \
    printf '%s  %s\n' "$HUB_SOURCE_SHA256" /opt/owrt-remote/owrt-remote-hub.py | sha256sum -c -
COPY vps/static/ /opt/owrt-remote/static/
COPY --chmod=755 docker/hub/askpass.sh /opt/owrt-remote/bin/askpass.sh
RUN groupadd --gid 23088 rohub && useradd --uid 23088 --gid 23088 --home-dir /opt/owrt-remote --shell /usr/sbin/nologin rohub && \
    mkdir -p /var/lib/owrt-remote && \
    chown -R 23088:23088 /opt/owrt-remote /var/lib/owrt-remote
USER 23088:23088
CMD ["python", "/opt/owrt-remote/owrt-remote-hub.py", "serve"]
