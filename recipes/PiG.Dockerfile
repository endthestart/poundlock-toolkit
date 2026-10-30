# PiG, the Go port of Pi, measured beside Pi on the same tasks (harness "pig").
# Supply a reviewed debian:bookworm-slim digest, never a moving tag, and the pig binary that
# recipes/fetch-pig.sh downloaded and checked against the release's published SHA-256:
#   sh recipes/fetch-pig.sh 0.5.3 1e45e5e591a06d087587d5f09d2b6d61673a1e262cb2609e6189defffd5ae7ec
#   docker build -f recipes/PiG.Dockerfile --build-arg DEBIAN_BASE=... .
ARG DEBIAN_BASE
FROM ${DEBIAN_BASE}
# `pig --version` prints <PiG release>+<Pi release>+<Pi commit>.
ARG PIG_VERSION=0.5.3+1.4.0+74f46864
COPY recipes/pig/pig /usr/local/bin/pig
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && chmod 0755 /usr/local/bin/pig \
    && test "$(pig --version)" = "${PIG_VERSION}"
# PiG runs Pi's TypeScript policy extension unchanged and needs no Node.js.
# The runner sets a non-root UID, isolated HOME, read-only root and bounded tmpfs.
