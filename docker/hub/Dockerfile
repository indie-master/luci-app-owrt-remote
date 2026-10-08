FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/owrt-remote
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates openssh-client iproute2 procps \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "pywebpush>=2,<3" "fido2>=2,<3"
COPY vps/owrt-remote-hub.py ./owrt-remote-hub.py
COPY vps/static ./static
COPY docker/hub/docker_hub.py ./docker_hub.py
RUN chmod 755 ./docker_hub.py && ln -s /opt/owrt-remote/docker_hub.py /usr/local/bin/owrt-hub
ENTRYPOINT ["owrt-hub"]
CMD ["serve"]
