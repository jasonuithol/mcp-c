FROM docker.io/nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

# Install Python + curl/ca-certs (curl needed for the model fetch below).
# mcp-knowledge-base is fetched as a release tarball over plain HTTPS
# (see requirements.txt), so no git client is required.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/mcp-knowledge

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

# Pre-fetch the all-MiniLM-L6-v2 embedding model so chromadb finds it
# locally — no host mount, no first-run download.
RUN mkdir -p /root/.cache/chroma/onnx_models/all-MiniLM-L6-v2 \
 && curl -fSL "https://chroma-onnx-models.s3.amazonaws.com/all-MiniLM-L6-v2/onnx.tar.gz" \
    | tar -xz -C /root/.cache/chroma/onnx_models/all-MiniLM-L6-v2

# Copy application code
COPY mcp-service.py .
COPY ingest/ ingest/

# Knowledge volume mount point
RUN mkdir -p /opt/knowledge

EXPOSE 5194

CMD ["python3", "mcp-service.py"]
