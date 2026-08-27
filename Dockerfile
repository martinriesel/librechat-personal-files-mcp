# Multi-stage build for librechat-personal-files-mcp
# Stage 1: builder
FROM python:3.12-slim AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libmagic-dev \
    && rm -rf /var/lib/apt/lists/*

# Create and activate virtual environment
ENV VENV_PATH=/opt/venv
RUN python -m venv $VENV_PATH
ENV PATH="$VENV_PATH/bin:$PATH"

# Install Python dependencies
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir ".[dev]"


# Stage 2: runtime
FROM python:3.12-slim AS runtime

# Install runtime dependencies (libmagic for MIME detection)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user and group
ARG UID=2000
ARG GID=2500
RUN groupadd -g $GID lcfiles && \
    useradd -u $UID -g $GID -m -s /bin/bash mcp

# Create storage directories with correct ownership
RUN mkdir -p /data/private /data/share /data/private/_system && \
    chown -R $UID:$GID /data/private /data/share

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Set working directory
WORKDIR /app

# Set umask for group-writable files
ENV UMASK=002

# Expose port
EXPOSE 8080

# Switch to non-root user
USER mcp

# Entry point
CMD ["personal-files-mcp"]