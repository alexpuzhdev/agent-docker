FROM node:22-bookworm-slim AS agents

ARG AGENT_UID=1000
ARG AGENT_GID=1000

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

RUN test "${AGENT_UID}" -gt 0 \
    && test "${AGENT_GID}" -gt 0 \
    && (getent group "${AGENT_GID}" > /dev/null || groupadd --gid "${AGENT_GID}" agents) \
    && usermod --uid "${AGENT_UID}" --gid "${AGENT_GID}" node \
    && install -d -m 0700 -o node -g "${AGENT_GID}" /home/node/.codex \
    && chmod 0700 /home/node \
    && printf '%s\n' 'cli_auth_credentials_store = "file"' > /home/node/.codex/config.toml \
    && chown "node:${AGENT_GID}" /home/node/.codex/config.toml \
    && chmod 0600 /home/node/.codex/config.toml \
    && mkdir -p /workspace

ENV HOME=/home/node \
    DISABLE_UPDATES=1
ENV PATH="${HOME}/.local/bin:${PATH}"
USER node
WORKDIR /workspace
CMD ["sleep", "infinity"]

FROM debian:bookworm-slim AS proxy

RUN apt-get update \
    && apt-get install -y --no-install-recommends tinyproxy \
    && rm -rf /var/lib/apt/lists/*
COPY --chmod=755 tinyproxy-start /usr/local/bin/tinyproxy-start
USER tinyproxy
CMD ["tinyproxy-start"]
