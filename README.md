# WareraNL Bot

Staging: [![CI / Deploy](https://github.com/colgre/wareraNL-bot/actions/workflows/deploy.yml/badge.svg?branch=staging)](https://github.com/colgre/wareraNL-bot/actions/workflows/deploy.yml)    
Live: [![CI / Deploy](https://github.com/colgre/wareraNL-bot/actions/workflows/deploy.yml/badge.svg)](https://github.com/colgre/wareraNL-bot/actions/workflows/deploy.yml)

WareraNL is a Discord bot implemented in Python using cogs for modular features.

## Repository layout

- `_api_keys.json` — local secret file for API keys (not tracked in VCS).
- `bot.py` — main entrypoint. Supports `--testing` and config/token overrides.
- `pyproject.toml` — Python project metadata and dependencies.
- `cogs/` — Discord cogs (feature modules).
- `templates/` — JSON/MD templates used by the standard_messages cogs.
- `database/` — SQLite schema and database backups.
- `services/` — service modules used by cogs (DB client, API client, workers).
- `verification_bot/` — verifier bot; see [verification_bot/README.md](verification_bot/README.md).

## Configuration

- `config/config.json` — main runtime config (`roles`, `channels`, colors, templates).
- `config/testing_config.json` — config for the test server.

## Setup

**1. Create a virtual environment**

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

**2. Create `.env`**

```bash
cp .env.example .env
```

| Variable | Description |
|----------|-------------|
| `TOKEN_TEST` | Token for the testing bot |
| `TOKEN_VERIFIER` | Token for the verifier bot |
| `VERIFIER_SECRET` | Shared secret — `python -c "import secrets; print(secrets.token_hex(32))"` |
| `VERIFIER_GUILD_ID` | Production server ID |
| `VERIFIER_ROLE_ID` | Nederlander role ID in the production server |

**3. Create `_api_keys.json`**

```json
{ "keys": ["your_api_key_here"] }
```

**4. Configure `config/config.json`**

Fill in `roles` and `channels` with numeric Discord IDs.

## Containers

All services use the `warera-nl:latest` image built from the repo root.

---

### discord-bot

War-guild testing bot. Uses `config/testing_config.json` and `TOKEN_TEST`.

```bash
# Recreate
docker build -t warera-nl:latest . && docker compose up -d --force-recreate discord-bot

# Logs
docker compose logs -f discord-bot

# Stop
docker compose stop discord-bot
```

---

### data-fetcher

Hourly background fetcher for citizen levels, MU memberships, etc. Writes to `database/external.db`.

```bash
# Recreate
docker build -t warera-nl:latest . && docker compose -f docker-compose.data-fetcher.yml up -d --force-recreate data-fetcher

# Logs
docker compose -f docker-compose.data-fetcher.yml logs -f data-fetcher

# Stop
docker compose -f docker-compose.data-fetcher.yml stop data-fetcher
```

---

### verifier-bot

Read-only bot in the production server. See [verification_bot/README.md](verification_bot/README.md).

```bash
# Recreate
docker build -t warera-nl:latest . && docker compose -f docker-compose.verifier.yml up -d --force-recreate verifier-bot

# Logs
docker compose -f docker-compose.verifier.yml logs -f verifier-bot

# Stop
docker compose -f docker-compose.verifier.yml stop verifier-bot
```

---

### rijksoverheid-web

Public website served on port `8484`.

```bash
# Recreate
docker build -t warera-nl:latest . && docker compose -f docker-compose.websites.yml up -d --force-recreate rijksoverheid-web

# Logs
docker compose -f docker-compose.websites.yml logs -f rijksoverheid-web

# Stop
docker compose -f docker-compose.websites.yml stop rijksoverheid-web
```

Environment overrides: `RW_HOST`, `RW_PORT` (default `8484`), `RW_DB_PATH`, `RW_CONFIG_PATH`.

---

## Running locally

```bash
python bot.py                          # production config
python bot.py --testing                # testing config + TOKEN_TEST
python bot.py --config my_config.json  # custom config
```

## Database & backups

- `database/schema.sql` — schema used to create the SQLite database.
- Backups in `database/` are kept as timestamped `.backup` files.

## Development notes

- Encapsulate new features in a Cog and register it in `bot.py`.
- Store role/channel IDs in `config.json`, never hardcode them.

## Contributing

Follow the codebase style, add tests for non-trivial logic, keep secrets out of commits.

