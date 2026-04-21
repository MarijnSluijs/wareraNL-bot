#!/usr/bin/env bash
set -e

cd /home/warera/staging/wareraNL-bot

git fetch origin staging
git reset --hard origin/staging
./.venv/bin/pip install -q -e .

sudo systemctl restart wareranl-bot-staging
sudo systemctl --no-pager --full status wareranl-bot-staging
