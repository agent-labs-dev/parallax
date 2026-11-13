# Two images from one file (box.py builds both):
#   --target runtime -> parallax-runtime:<version>: python, node and the two users, no parallax.
#                       Box images install their harness on top of this, so a harness install
#                       layer stays cached across parallax source changes.
#   (default)        -> parallax-base:<version>-<src8>: runtime + the parallax wheel; the sidecar.
# Build context: a directory holding px_clock.c and (default target only) one parallax wheel and
# constraints.txt, the dependency versions the host runs, written by box.build_with_wheel.
ARG NODE_IMAGE=node:24.22.2-bookworm-slim
FROM ${NODE_IMAGE} AS node

# px-clock runs box processes on the world's clock (see px_clock.c); built against the runtime's libc.
FROM python:3.13-slim AS clock
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/*
COPY px_clock.c /tmp/px_clock.c
RUN gcc -O2 -Wall -Werror -o /usr/local/bin/px-clock /tmp/px_clock.c

FROM python:3.13-slim AS runtime
COPY --from=clock /usr/local/bin/px-clock /usr/local/bin/px-clock
# Node from the official image rather than a distro package: harness CLIs need a current LTS.
# Only node and its bundled npm are copied, so python's /usr/local is left intact.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# agent: runs harnesses in boxes. world: runs the world server and meter in the sidecar.
RUN useradd --create-home --home-dir /home/agent --shell /bin/sh agent \
 && useradd --create-home --home-dir /home/world --shell /bin/sh world \
 && mkdir /workspace && chown agent:agent /workspace \
 && mkdir /etc/parallax && echo 0 > /etc/parallax/clock

FROM runtime AS base
COPY *.whl constraints.txt /tmp/wheels/
# Every dependency at the host's version, so the sidecar runs (and prices) what the host does.
RUN pip install --no-cache-dir -c /tmp/wheels/constraints.txt /tmp/wheels/*.whl \
 && pip check \
 && rm -rf /tmp/wheels
USER world
WORKDIR /home/world
