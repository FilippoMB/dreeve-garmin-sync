# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

FROM ${PYTHON_IMAGE} AS builder

WORKDIR /build

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM ${PYTHON_IMAGE}

ARG BUILD_DATE=unknown
ARG VERSION=unknown
ARG VCS_REF=unknown

LABEL org.opencontainers.image.title="dreeve-garmin-sync" \
      org.opencontainers.image.description="Garmin Connect activity synchronization add-on for Dreeve" \
      org.opencontainers.image.url="https://github.com/FilippoMB/dreeve-garmin-sync" \
      org.opencontainers.image.source="https://github.com/FilippoMB/dreeve-garmin-sync" \
      org.opencontainers.image.documentation="https://github.com/FilippoMB/dreeve-garmin-sync#readme" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"

ENV HOME=/tmp \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.lock /tmp/requirements.lock
RUN python -m pip install \
    --no-cache-dir \
    --only-binary=:all: \
    --require-hashes \
    --requirement /tmp/requirements.lock \
    && rm /tmp/requirements.lock

COPY --from=builder /wheels /tmp/wheels
RUN python -m pip install --no-cache-dir --no-deps /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels \
    && garmin-sync --version

USER 1000:1000

HEALTHCHECK --interval=1m --timeout=30s --start-period=30s --retries=3 \
    CMD ["garmin-sync", "status", "--healthcheck"]

ENTRYPOINT ["garmin-sync"]
CMD ["daemon"]
