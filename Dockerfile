# Use the multi-platform digest for python:3.11-slim
FROM python:3.11-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff AS builder

WORKDIR /src
COPY . /src/

# Build the wheel
RUN pip install --no-cache-dir build && \
    python -m build --wheel --outdir /dist


FROM python:3.11-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff

# Install curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Create a non-root user
RUN groupadd -r agentos && useradd -r -g agentos agentos

# Create data and workspace directories with appropriate ownership
RUN mkdir /data /workspace && chown agentos:agentos /data /workspace

# Set workdir
WORKDIR /app

# Configure default paths to the writable volumes
ENV AGENT_OS_CHECKPOINTS_DB=/data/checkpoints.db
ENV AGENT_OS_VAULT_PATH=/workspace
ENV AGENT_OS_SANDBOX=/workspace

# Copy the built wheel from the builder stage
COPY --from=builder /dist/*.whl /tmp/wheel/

# Install the wheel with [serve] extra
RUN WHEEL=$(ls /tmp/wheel/*.whl | head -n 1) && \
    pip install --no-cache-dir "${WHEEL}[serve]" && \
    rm -rf /tmp/wheel

# Switch to non-root user
USER agentos

# Expose API port
EXPOSE 4680

# Health check against the API endpoint
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl --fail http://127.0.0.1:4680/api/health || exit 1

# Run the server on 0.0.0.0 for container networking
CMD ["agent-os", "serve", "--host", "0.0.0.0", "--port", "4680"]
