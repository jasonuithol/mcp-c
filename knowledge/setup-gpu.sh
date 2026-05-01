#!/usr/bin/env bash
# setup-gpu.sh — install NVIDIA Container Toolkit for GPU-accelerated embeddings
# Run once on a fresh machine. Requires sudo.
set -euo pipefail

echo "=== NVIDIA Container Toolkit Setup ==="
echo "This installs the toolkit so podman can pass your GPU into containers."
echo ""

if ! nvidia-smi &>/dev/null; then
    echo "ERROR: nvidia-smi not found. Install NVIDIA drivers first."
    exit 1
fi

GPU=$(nvidia-smi --query-gpu=name --format=csv,noheader)
echo "Detected GPU: $GPU"
echo ""

sudo mkdir -p /usr/share/keyrings /etc/cdi

echo "Adding NVIDIA container toolkit repository..."
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list > /dev/null

echo "Installing nvidia-container-toolkit..."
sudo apt-get update -qq
sudo apt-get install -y nvidia-container-toolkit

echo "Generating CDI spec..."
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml

echo ""
echo "=== Verification ==="
nvidia-ctk cdi list | grep -i nvidia && echo "CDI devices registered." || echo "WARNING: No CDI devices found."
echo ""
echo "Done. GPU is now available to podman containers via --device nvidia.com/gpu=all"
