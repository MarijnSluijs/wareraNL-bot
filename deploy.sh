#!/usr/bin/env bash
set -e

cd /home/warera/live/wareraNL-bot

git pull origin main
./.venv/bin/pip install -e .

sudo systemctl restart wareranl-bot
sudo systemctl --no-pager --full status wareranl-bot
