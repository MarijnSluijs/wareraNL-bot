#!/usr/bin/env bash
set -e

cd /home/warera/staging/wareraNL-bot

git pull origin staging
./.venv/bin/pip install -q -e .

sudo systemctl restart wareranl-bot-staging
sudo systemctl --no-pager --full status wareranl-bot-staging
