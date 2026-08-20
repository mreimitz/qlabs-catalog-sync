# syntax=docker/dockerfile:1
#
# QLabs Catalog Sync -- container image. T9.1 / WP9.
#
# ==============================================================================================
# What this image actually runs today, and what it cannot yet run
# ==============================================================================================
#
# RS-07 section 1 and section 6 (planning/Research/RS-07-architecture-techstack-references/
# outputs/architecture-and-techstack.md) describe the target shape: "One container image, one
# long-running process. Entry point starts the async event loop, loads and validates config,
# opens the state store, registers per-pair sync jobs with the scheduler, and exposes a small
# HTTP surface for /healthz and /metrics."
#
# That long-running entry point does not exist yet. `packages/qlabs-catalog-sync/src/
# qlabs_catalog_sync/scheduler.py` (T2.6, `SyncScheduler`) and `.../observability.py`'s
# `ObservabilityServer` (T2.7) are both fully implemented and tested, but nothing in
# `cli/app.py` wires them together -- the CLI registers exactly three commands (`run`,
# `dry-run`, `identity-confirm`), all of which are documented, one-shot batch operations:
# `execute_cycles` (cli/sync_commands.py) runs exactly one cycle per selected pair/entity-type
# combination and returns; it never touches `SyncScheduler` or `ObservabilityServer`. Grep
# confirms neither class is constructed anywhere outside the two packages' own tests.
# `cli/wiring.py`'s module docstring describes exactly how a scheduler-backed command should
# reuse `build_connector_pool`/`build_sync_loop`, but that command itself was never written.
#
# T9.1 owns only `Dockerfile` and `.dockerignore` -- not `cli/`, where that command would live
# -- so it cannot be added here. The concrete, still-needed change: a new CLI command (e.g.
# `serve`), most naturally in a new `packages/qlabs-catalog-sync/src/qlabs_catalog_sync/cli/
# serve_command.py` registered via `cli.add_command(serve)` in `cli/app.py`, that (1) builds the
# connector pool and one `SyncLoop` per configured pair exactly as `execute_cycles` does, (2)
# hands them to `scheduler.SyncScheduler` and calls `.start()`, (3) starts an
# `observability.ObservabilityServer` bound to a real port, (4) installs SIGTERM/SIGINT handlers
# that call `SyncScheduler.shutdown()` (which itself waits for an in-flight cycle -- see that
# method's docstring -- rather than cutting one off mid-write) followed by
# `ObservabilityServer.stop()`, and then (5) blocks (e.g. `await asyncio.Event().wait()`) until
# shutdown completes. Until that command exists, this image can run `run` (one pass over every
# configured pair, then exits -- suitable as a Kubernetes Job/CronJob, not a Deployment),
# `dry-run`, or `identity-confirm`, faithfully and correctly, but it cannot run the persistent
# service RS-07 describes and this Dockerfile does not pretend otherwise: there is no shell loop
# here wrapping `run` in a fake `while true` to simulate a daemon. `ENTRYPOINT`/`CMD` (bottom of
# this file) are deliberately built so that adding `serve` later needs no Dockerfile change
# beyond the default `CMD`.
#
# ==============================================================================================
# Which packages this image installs, and why Collibra/Snowflake are excluded
# ==============================================================================================
#
# The engine discovers connectors purely through the `qlabs_catalog_sync.connectors` entry-point
# group (discovery.py) -- a connector that is not installed simply does not exist to it, with no
# error logged. This image installs exactly the four packages the MVP (CLAUDE.md's "What ships
# first": RM-01, a one-way Databricks-to-Qlik sync) needs: the SDK, the engine, the Qlik
# connector (the only write target in v1), and the Databricks connector (the only source
# connector RM-01 uses). `qlabs-connector-collibra` and `qlabs-connector-snowflake` are RM-05
# ("Track B"), explicitly blocked until v0.1 tags (CLAUDE.md), and their `Connector` classes
# today are literal placeholders that do not subclass the SDK's `Connector` ABC (see each
# package's `src/*/__init__.py` -- "Placeholder ... connector", "TODO(T5.x)"/"TODO(T6.x)").
# discovery.py's own module docstring names this exact scenario: an installed-but-unimplemented
# connector fails the SDK contract-version gate and is logged at *error* level on every single
# process start (`connector_discovery_load_failed` / `connector_discovery_contract_incompatible`
# in discovery.py), even though nothing ever configures a pair against it. Installing either
# package here would mean every boot of this image prints error-level log lines about
# connectors no RM-01 config ever names -- a real, if cosmetic, operational nuisance -- for zero
# capability gained. Excluding them (they are simply never `uv sync`'d into this image) means
# they never appear in `importlib.metadata.entry_points()` at all, so discovery never attempts
# to load them and never logs anything about them. RM-05 landing is the natural point to build a
# second image variant (or a build arg toggling which `--package` flags below are passed).
#
# Two packaging bugs this image originally had to work around were fixed in the packages
# themselves instead: the SDK imported its test kit eagerly (so respx/pytest were needed by
# every production install), and the Alembic scripts never shipped in the wheel (so no real
# install could migrate). Both now work in a plain `pip install` of the built wheels.

FROM python:3.11.13-slim-bookworm AS builder

# Pinned uv, copied from its own minimal image rather than `pip install uv` (no extra apt/pip
# bootstrap needed in this stage just to get the tool that does everything else).
COPY --from=ghcr.io/astral-sh/uv:0.10.2 /uv /uvx /usr/local/bin/

ENV UV_FROZEN=1 \
    UV_NO_DEV=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=python3.11 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /src

# --- Dependency layer -------------------------------------------------------------------------
# Only the workspace root + the four target packages' *manifests* (no application source yet),
# so an edit to application code never invalidates this layer. `--no-install-workspace` installs
# every third-party dependency from the lock without needing any package's `src/` present.
COPY pyproject.toml uv.lock ./
COPY packages/qlabs-catalog-sync-sdk/pyproject.toml packages/qlabs-catalog-sync-sdk/pyproject.toml
COPY packages/qlabs-catalog-sync/pyproject.toml packages/qlabs-catalog-sync/pyproject.toml
COPY packages/qlabs-connector-qlik/pyproject.toml packages/qlabs-connector-qlik/pyproject.toml
COPY packages/qlabs-connector-databricks/pyproject.toml packages/qlabs-connector-databricks/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-workspace \
        --package qlabs-catalog-sync \
        --package qlabs-connector-qlik \
        --package qlabs-connector-databricks

# --- Application layer -------------------------------------------------------------------------
# Now the real source for exactly those four packages (never `qlabs-connector-collibra` /
# `qlabs-connector-snowflake` -- see the header comment), and install them non-editable so the
# runtime stage needs nothing but the venv this produces.
COPY packages/qlabs-catalog-sync-sdk/src packages/qlabs-catalog-sync-sdk/src
COPY packages/qlabs-catalog-sync/src packages/qlabs-catalog-sync/src
COPY packages/qlabs-connector-qlik/src packages/qlabs-connector-qlik/src
COPY packages/qlabs-connector-databricks/src packages/qlabs-connector-databricks/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-editable \
        --package qlabs-catalog-sync \
        --package qlabs-connector-qlik \
        --package qlabs-connector-databricks

# ------------------------------------------------------------------------------------------------
# Stage 2: runtime -- bare interpreter + the venv above. No uv, no build tools, no source tree.
# ------------------------------------------------------------------------------------------------
FROM python:3.11.13-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="qlabs-catalog-sync" \
      org.opencontainers.image.description="QLabs Catalog Sync -- one-way Databricks-to-Qlik metadata sync (RM-01 MVP)"

# Fixed, documented user/group -- see the header comment on why the uid/gid are pinned. Plain
# (not --system) useradd/groupadd: uid/gid 1000 is the conventional first-regular-user id, and
# --system would only warn that 1000 sits outside its own reserved low-uid range.
RUN groupadd --gid 1000 qlabs \
    && useradd --uid 1000 --gid qlabs --home-dir /app --no-create-home \
        --shell /usr/sbin/nologin qlabs \
    && mkdir -p /app /data \
    && chown -R qlabs:qlabs /app /data

COPY --from=builder --chown=qlabs:qlabs /opt/venv /opt/venv

WORKDIR /app

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # SQLite state store + identity-review file both live on the /data volume owned by the
    # `qlabs` user above; `cli/app.py` already reads both these exact env var names, so no code
    # change is needed to honor a volume mounted at /data. (sqlite:////<path> is SQLAlchemy's
    # absolute-path form -- three slashes for the sqlite:// scheme, a fourth to start the path.)
    QLABS_STATE_DB="sqlite:////data/qlabs-catalog-sync.db" \
    QLABS_IDENTITY_REVIEW_FILE="/data/identity-review.json" \
    # This Dockerfile's own convention for the observability HTTP surface's port -- see the
    # header comment: no code reads this yet.
    QLABS_OBSERVABILITY_PORT=8080

# Persistent state: the SQLite database and the identity-review file. Mount a real volume here
# (a named volume, a host bind mount, or a Kubernetes PVC) writable by uid:gid 1000:1000, or
# everything in it is lost when the container is removed.
VOLUME ["/data"]

# ObservabilityServer's /healthz + /metrics surface (observability.py), once something starts it.
EXPOSE 8080

USER qlabs:qlabs

# See the header comment: this will report unhealthy under every command this image can
# currently run (none of them bind $QLABS_OBSERVABILITY_PORT). It starts being meaningful the
# moment a long-running `serve` command exists and binds ObservabilityServer to that port.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('QLABS_OBSERVABILITY_PORT', '8080') + '/healthz', timeout=2)"]

# Exec form, naming the console script directly -- see the header comment's "Signals" note.
ENTRYPOINT ["qlabs-catalog-sync"]
# No subcommand is assumed by default: `run` performs one pass over every configured pair and
# exits (see the header comment's first section) and defaulting to it here would make a plain
# `docker run qlabs-catalog-sync` silently look like a persistent service when it is not one.
# An operator (or orchestrator manifest) supplies the real subcommand explicitly, e.g.:
#   docker run --rm -v ./config.yaml:/etc/qlabs-catalog-sync/config.yaml:ro \
#     -v qlabs-data:/data -e QLIK__ACME__CLIENT_SECRET=... qlabs-catalog-sync \
#     run --config /etc/qlabs-catalog-sync/config.yaml
CMD ["serve", "--config", "/etc/qlabs-catalog-sync/config.yaml"]
