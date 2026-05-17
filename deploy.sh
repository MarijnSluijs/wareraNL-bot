#!/usr/bin/env bash
set -e

cd /home/warera/live/wareraNL-bot

# Preserve live-edited template files that are managed by bot commands
cp templates/mus.json /tmp/mus.json.bak 2>/dev/null || true
cp templates/roles.json /tmp/roles.json.bak 2>/dev/null || true

git fetch origin main
git reset --hard origin/main

# Restore live-edited mus.json (bot commands write MU data to this file)
cp /tmp/mus.json.bak templates/mus.json 2>/dev/null || true

# Merge saved role_id values back into the freshly pulled roles.json.
# This preserves role IDs for existing buttons while still picking up new
# buttons / description changes from git (new buttons keep role_id=0 and
# are auto-created when /generalroles is next run).
if [ -f /tmp/roles.json.bak ]; then
    ./.venv/bin/python3 - <<'PYEOF'
import json, sys

with open("/tmp/roles.json.bak") as f:
    backup = json.load(f)
with open("templates/roles.json") as f:
    fresh = json.load(f)

# Build label -> role_id lookup from backup
saved_ids: dict = {}
for embed in backup.get("embeds", []):
    for btn in embed.get("buttons", []):
        label = btn.get("label")
        role_id = btn.get("role_id") or 0
        if label and role_id:
            saved_ids[label] = role_id

# Apply saved IDs to fresh template (new buttons keep role_id=0)
for embed in fresh.get("embeds", []):
    for btn in embed.get("buttons", []):
        label = btn.get("label")
        if label in saved_ids:
            btn["role_id"] = saved_ids[label]

with open("templates/roles.json", "w") as f:
    json.dump(fresh, f, indent=2, ensure_ascii=False)
    f.write("\n")
PYEOF
fi

./.venv/bin/pip install -e .

sudo systemctl restart wareranl-bot
sudo systemctl restart wareranl-web
sudo systemctl --no-pager --full status wareranl-bot
