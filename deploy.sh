#!/usr/bin/env bash
set -e

cd /root/warera/wareraNL-bot

git pull origin main
./.venv/bin/pip install -r requirements.txt

sudo systemctl restart wareranl-bot
sudo systemctl --no-pager --full status wareranl-bot
