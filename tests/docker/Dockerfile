FROM python:3.11-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl iproute2 util-linux \
    && rm -rf /var/lib/apt/lists/*
