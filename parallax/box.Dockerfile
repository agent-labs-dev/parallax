# parallax-box-<harness>:<version>-<sha8> - one image per (harness install line, parallax source).
# Build context: a directory holding one parallax wheel.
ARG RUNTIME
FROM ${RUNTIME}
ARG HARNESS_INSTALL=""
# The install line comes from the harness manifest (trusted repository data), so it is a shell RUN.
# It precedes the wheel so a parallax source change reuses this layer from the build cache.
RUN if [ -n "$HARNESS_INSTALL" ]; then sh -c "$HARNESS_INSTALL"; fi
# In-box workers (`python -m parallax.world bridge`, the pi relay, the agents-sdk worker) are parallax code.
COPY *.whl /tmp/wheels/
RUN pip install --no-cache-dir /tmp/wheels/*.whl && rm -rf /tmp/wheels
USER agent
ENV HOME=/home/agent
# The workspace is evidence, and bytecode the agent's own test runs leave in it is not text a judge can read.
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /workspace
