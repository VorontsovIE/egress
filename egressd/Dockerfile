# egressd image (spec section 44: egressd/ is built into the compose stack).
# Minimal in A1: health/ready/self endpoints + client-token auth. The full
# /v1 API (routes, defaults, one-shot, probes, benchmarks) is phase A3 and
# extends the same package.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY egressd/pyproject.toml /app/pyproject.toml
COPY egressd/src /app/src
# Provider adapters (spec section 11.2) are imported by egressd for the
# GEO/provider update paths; keep them importable from the image root.
COPY adapters /app/adapters

# /app on the path so `import adapters` resolves at runtime (the egressd
# console script lives in site-packages, not /app).
ENV PYTHONPATH=/app

RUN pip install --no-cache-dir /app

# Non-root: egressd needs no privileges (no TUN, no raw sockets).
USER 1000:1000

EXPOSE 9091
CMD ["egressd"]
