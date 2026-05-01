FROM debian:bookworm-slim

LABEL maintainer="mcp-c"
LABEL description="C build + test + analyze tools MCP server"

# Toolchain: gcc 12 (default in bookworm), clang 14, clang-tidy, clang-format,
# cmake, meson, ninja, make, valgrind, cppcheck. Plus python3 for the MCP
# service itself, curl/ca-certs for fetching the mcp-knowledge-base tarball.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc g++ \
        clang clang-tidy clang-format \
        cmake meson ninja-build make \
        valgrind cppcheck \
        gdb \
        pkg-config \
        python3 python3-pip \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python deps for the MCP service. The service is small; pin only what we need.
RUN pip install --no-cache-dir --break-system-packages \
        fastmcp \
        "mcp-knowledge-base @ https://github.com/jasonuithol/mcp-knowledge-base/archive/refs/tags/v0.2.1.tar.gz"

WORKDIR /app
COPY mcp-service.py .

EXPOSE 5192

CMD ["python3", "mcp-service.py"]
