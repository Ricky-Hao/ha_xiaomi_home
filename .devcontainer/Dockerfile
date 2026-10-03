# Public development image. Never pass credentials as build arguments or ENV.
FROM node:22.22.0-bookworm-slim AS node
FROM mcr.microsoft.com/devcontainers/python:1-3.13-bookworm

USER root
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -sfn /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sfn /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# Fixed public releases; no private registry or patched binary fallback.
RUN npm install --prefix /opt/acp --no-audit --no-fund \
        acpx@0.19.3 opencode-ai@1.18.34 \
    && npm cache clean --force
COPY acp/runtime.py /opt/acp/runtime.py
COPY acp/launch.sh /opt/acp/launch.sh
RUN chmod 0755 /opt/acp/launch.sh \
    && ln -s /opt/acp/launch.sh /usr/local/bin/acpx \
    && ln -s /opt/acp/launch.sh /usr/local/bin/opencode

USER vscode
WORKDIR /workspaces/ha_xiaomi_home
