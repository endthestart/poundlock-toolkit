# Supply a reviewed node:22-bookworm-slim digest, never a moving tag.
# Build from the repository root: docker build -f recipes/Pi.Dockerfile --build-arg NODE_BASE=... .
ARG NODE_BASE
FROM ${NODE_BASE}
ARG PI_VERSION=1.1.0
# Pi and everything it depends on come from recipes/pi/package-lock.json.
COPY recipes/pi/package.json recipes/pi/package-lock.json /opt/pi/
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm ci --prefix /opt/pi --ignore-scripts --omit=optional --omit=dev \
    && ln -s /opt/pi/node_modules/.bin/pi /usr/local/bin/pi \
    && test "$(pi --version)" = "${PI_VERSION}" \
    && npm cache clean --force \
    && rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
        /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack
# npm is needed only to install. Pi calls it only for `pi install`, which the runner never
# uses, and npm's bundled packages carry most of the image's scanner findings.
# The runner sets a non-root UID, isolated HOME, read-only root and bounded tmpfs.
