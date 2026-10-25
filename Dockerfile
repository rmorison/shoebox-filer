FROM python:3.11-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Two-phase sync: dependencies first (cache-friendly, no project sources yet),
# then the project itself. hatchling needs README.md to build the package.
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project

COPY README.md ./
COPY src/ src/
RUN uv sync --no-dev --frozen

FROM python:3.11-slim-bookworm

# Pinned third-party binaries (linux/amd64 — the deployment target).
# supercronic: version + sha256 from the upstream GitHub release. Upstream
#   publishes a SHA1 in the release notes; the sha256 below is of the artifact
#   whose SHA1 matches that published value.
#   https://github.com/aptible/supercronic/releases
# rclone: sha256 from https://downloads.rclone.org/v1.75.0/SHA256SUMS
#   ARG names carry a PIN_ prefix because rclone parses ANY RCLONE_* env var
#   as CLI configuration (RCLONE_VERSION=1.75.0 → invalid --version flag).
ARG SUPERCRONIC_VERSION=v0.2.48
ARG SUPERCRONIC_SHA256=88c1b66b94c486f972fdd1a4d1f901e3e75ff04f749cddd60c5db573e3a33c6c
ARG PIN_RCLONE_VERSION=1.75.0
ARG PIN_RCLONE_SHA256=266598b5c66a42b821571332013cc85a88ffcff8939cf0643861c8d033dd3a4a

RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid 1000 --create-home appuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH"

# Browsers live outside the venv so every user (uid 1000 at runtime) can read
# them; the renderer defaults to a CWD-relative path when this is unset.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# System packages, in one layer:
#   - weasyprint runtime libs, per the WeasyPrint docs for Debian >= 11
#     (libpango-1.0-0, libpangoft2-1.0-0, libharfbuzz-subset0) plus fonts for
#     the plain-text receipt template, which asks for a monospace face
#   - postgresql-client-18 from PGDG: bookworm ships client v15, too old to
#     pg_dump the v18 production server (nightly backup job)
#   - Chromium (headless shell only) and its OS dependencies, installed with
#     the *venv's* playwright CLI so the browser build always matches the
#     pinned pip `playwright` version in uv.lock. Bumping that pin REQUIRES
#     rebuilding this image: Playwright refuses to launch a browser revision
#     it did not install.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/keyrings/pgdg.asc; \
    echo "deb [signed-by=/usr/share/keyrings/pgdg.asc]" \
        "https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        fontconfig \
        fonts-dejavu-core \
        libharfbuzz-subset0 \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        postgresql-client-18; \
    /app/.venv/bin/playwright install --with-deps --only-shell chromium; \
    chmod -R a+rX "${PLAYWRIGHT_BROWSERS_PATH}"; \
    rm -rf /var/lib/apt/lists/*

# Scheduler (supercronic) and Dropbox mirror (rclone) binaries. Both are
# checksum-verified; a version bump must bring its checksum with it.
RUN set -eux; \
    curl -fsSL -o /usr/local/bin/supercronic \
        "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64"; \
    echo "${SUPERCRONIC_SHA256}  /usr/local/bin/supercronic" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/supercronic; \
    supercronic -version; \
    curl -fsSL -o /tmp/rclone.deb \
        "https://downloads.rclone.org/v${PIN_RCLONE_VERSION}/rclone-v${PIN_RCLONE_VERSION}-linux-amd64.deb"; \
    echo "${PIN_RCLONE_SHA256}  /tmp/rclone.deb" | sha256sum -c -; \
    dpkg --install /tmp/rclone.deb; \
    rm -f /tmp/rclone.deb; \
    rclone version

USER appuser

ENTRYPOINT ["receipt-index"]
