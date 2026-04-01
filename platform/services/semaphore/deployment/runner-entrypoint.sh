#!/bin/bash
# runner-entrypoint.sh — Install hvac and hashi_vault collection on Semaphore runner boot.
# Mount this script into the runner container and set as entrypoint.
# Adds ~10-30s to container startup but survives image updates.
set -e

echo "[runner-entrypoint] Installing hvac..."
pip install --quiet --no-cache-dir 'hvac>=2.0.0' 2>/dev/null

echo "[runner-entrypoint] Installing community.hashi_vault collection..."
ansible-galaxy collection install community.hashi_vault --force-with-deps 2>/dev/null || true

echo "[runner-entrypoint] Dependencies ready."
exec "$@"
