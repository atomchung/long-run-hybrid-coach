# Deployment artifact for the Coach Gateway (garmin_coach_loop/gateway.py) -- the HTTP
# transport that serves many OAuth-connected athletes from one process. Nothing else in
# this repository is imported at gateway runtime -- not the CLI's other commands' own
# dependencies, not scripts/, not contracts/, not examples/ -- so nothing else is copied
# into the image. (Confirmed by grep: no module under garmin_coach_loop/ imports scripts.)
#
# Platform-neutral by design (see docs/deploy-gateway.md): this file carries no
# Fly-specific instructions. fly.toml is what makes Fly the default target; a different
# host only needs its own equivalent of that file, pointed at this same image.
FROM python:3.11-slim

# The product is stdlib-only by repository rule (AGENTS.md: must not call an LLM API, and
# there is no requirements.txt here) -- no `pip install` step exists because none is
# needed, not because one was forgotten. These are OS packages, not Python ones, so
# neither contradicts "no pip install" -- they supply data and trust anchors the stdlib
# itself reads, not a library:
#   - tzdata: `python:3.11-slim`'s Debian base ships without system timezone data, so
#     zoneinfo.ZoneInfo(...) (context_core.py, gateway.py, store.py) raises
#     ZoneInfoNotFoundError for every real IANA name -- including DEFAULT_TIMEZONE itself
#     ("Asia/Taipei", context_core.py), so this is not an edge case an operator might avoid
#     by always passing UTC.
#   - ca-certificates: without it, every outbound HTTPS call this gateway makes --
#     source_intervals.py's reads, the OAuth token exchange, delivery writes -- fails
#     verification against intervals.icu's real certificate.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY garmin_coach_loop/ /app/garmin_coach_loop/

# Unbuffered so LOGGER.info(...) output (gateway.py) reaches the platform's log collector
# as it is written, rather than sitting in a pipe buffer until the process exits.
ENV PYTHONUNBUFFERED=1

# Documentary only -- Docker does not enforce this. The bound port is decided at startup
# by GARMIN_COACH_LOOP_GATEWAY_PORT, with 8422 (gateway.py DEFAULT_PORT) as the fallback
# when nothing sets it; keep this in sync with fly.toml's internal_port.
EXPOSE 8422

# GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT must resolve outside /app: store.py's own
# "state directory must be outside the repository" guard treats this image's WORKDIR as
# the repository, because it is computed from garmin_coach_loop/store.py's own install
# path (two parents up) with no way to tell a real checkout from a container image apart.
# A volume mounted anywhere under /app -- see fly.toml's [mounts] -- fails startup even
# though the mount itself succeeded.
#
# Exec form, not shell form: this runs python3 as PID 1 directly, with no /bin/sh in
# between. Shell form (`CMD python3 -m ...`) would run python3 as a *child* of a shell,
# and SIGTERM would stop at that shell -- never reaching the handler run_gateway()
# (gateway.py) registers for a clean, request-draining shutdown.
CMD ["python3", "-m", "garmin_coach_loop.cli", "serve-gateway"]
