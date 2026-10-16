# paperbridge, built by .github/workflows/paperbridge.yml on vX.Y.Z tags.
FROM python:3.15.0-slim-trixie@sha256:f48fda6d59b2b3437781d1c3ccf4aa0e26ae871de3cdd758b49ab45ee7291619

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
