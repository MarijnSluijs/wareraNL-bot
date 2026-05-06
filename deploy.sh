#!/usr/bin/env bash
set -e

cd /home/warera/live/wareraNL-bot

# Preserve live-edited template files that are managed by bot commands
cp templates/mus.json /tmp/mus.json.bak 2>/dev/null || true
cp templates/roles.json /tmp/roles.json.bak 2>/dev/null || true

git fetch origin main
git reset --hard origin/main

# Restore live-edited files (bot commands write to these; git must not overwrite them)
cp /tmp/mus.json.bak templates/mus.json 2>/dev/null || true
cp /tmp/roles.json.bak templates/roles.json 2>/dev/null || true

./.venv/bin/pip install -e .

sudo systemctl restart wareranl-bot
sudo systemctl restart wareranl-web
sudo systemctl --no-pager --full status wareranl-bot
