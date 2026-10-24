# paperbridge, built by .github/workflows/paperbridge.yml on vX.Y.Z tags.
FROM python:3.15.1-slim-trixie@sha256:533b0b2f9b5d46fd097091cf826f27902f1d6c3bca714b6dc74551356aa97a51

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
