#!/usr/bin/env bash
set -e

cd /home/warera/live/wareraNL-bot

git fetch origin main
git reset --hard origin/main
./.venv/bin/pip install -e .

sudo systemctl restart wareranl-bot
sudo systemctl restart wareranl-web
sudo systemctl --no-pager --full status wareranl-bot
