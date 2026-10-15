# paperbridge, built by .github/workflows/paperbridge.yml on vX.Y.Z tags.
FROM python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --root-user-action=ignore . && rm -rf /src

# No storage of its own, so the UID only has to be non-root; 10004 continues the series
# in host/00-prep.sh.
USER 10004:10004
WORKDIR /
ENTRYPOINT ["paperbridge"]
