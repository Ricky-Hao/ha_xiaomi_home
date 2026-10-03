# Reuse the exact existing patched toolset. No compilation or npm fallback.
# ACP_TOOLCHAIN_IMAGE is a registry/repository, NOT a credential or tagged ref.
ARG ACP_TOOLCHAIN_IMAGE
FROM ${ACP_TOOLCHAIN_IMAGE}@sha256:4c286793e2aa660accdb8f9c5836b19a24908c5dae6132583a15b0edb39e80dc AS patched
FROM mcr.microsoft.com/devcontainers/python:1-3.13-bookworm

USER root
# Copy only tools and provenance, never source-image HOME, config or build.log.
COPY --from=patched /opt/network-acp/patched/39caca86b63514b024ce96d503721a5863442b56a171dc9cfecbfe06f34f4bf7/node/ /opt/acp/toolset/node/
COPY --from=patched /opt/network-acp/patched/39caca86b63514b024ce96d503721a5863442b56a171dc9cfecbfe06f34f4bf7/node_modules/ /opt/acp/toolset/node_modules/
COPY --from=patched /opt/network-acp/patched/39caca86b63514b024ce96d503721a5863442b56a171dc9cfecbfe06f34f4bf7/build.json /opt/acp/toolset/build.json
COPY acp/runtime.py acp/launch.sh acp/opencode.template.json /opt/acp/
RUN chmod -R a+rX /opt/acp/toolset \
    && chmod 0755 /opt/acp/launch.sh \
    && ln -s /opt/acp/launch.sh /usr/local/bin/acpx \
    && ln -s /opt/acp/launch.sh /usr/local/bin/opencode \
    && ln -s /opt/acp/toolset/node/bin/node /usr/local/bin/node \
    && python3 -B /opt/acp/runtime.py verify

USER vscode
WORKDIR /workspaces/ha_xiaomi_home
