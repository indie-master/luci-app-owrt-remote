FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/owrt-remote
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client curl ca-certificates iproute2 procps && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pywebpush fido2
COPY vps/owrt-remote-hub.py ./owrt-remote-hub.py
COPY vps/static ./static
COPY docker/hub/systemctl /usr/local/bin/systemctl
RUN chmod 755 /usr/local/bin/systemctl
CMD ["python", "/opt/owrt-remote/owrt-remote-hub.py", "serve"]
