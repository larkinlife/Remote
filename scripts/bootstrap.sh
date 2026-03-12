#!/bin/bash
# Bootstrap - runs ONCE on workspace creation
set -e

echo "[bootstrap] Starting bootstrap..."

# Install playwright globally for MCP
npm install -g @playwright/mcp@0.0.68 2>/dev/null || true

# Create marker
touch /home/user/.bootstrap_done
echo "[bootstrap] Bootstrap complete."
