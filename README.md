# WareraNL Bot

WareraNL is a Discord bot implemented in Python using cogs for modular features. This README explains the repository layout, purpose of the main folders and cogs, and how to run the bot in both production and testing modes.

## Repository layout

- `_api_keys.json` — local secret file for API keys (not tracked in VCS). Keep private.
- `config.json` — main runtime configuration with `roles`, `channels`, colors and message templates.
- `testing_config.json` — example/template config for a testing server (fill with IDs for your test guild).
- `bot.py` — main entrypoint. Supports `--testing` and config/token overrides.
- `pyproject.toml` — Python project metadata and dependencies. Core deps under `[project]`, website extras under `[project.optional-dependencies] website`.

- `cogs/` — Discord cogs (feature modules) loaded by the bot. See below for details.
- `templates/` — JSON/MD templates used by the `standard_messages` cogs.
- `database/` — SQLite schema and database backups.
- `logs/` — runtime logs (configured to use relative paths so the repo is portable).
- `scripts/` — small helper scripts and one-off runners used during development.
- `services/` — small service modules used by scripts and cogs (DB client, API client, workers).

## Cogs (features)

- `cogs/embeds.py` — helpers for building rich Discord embeds used across cogs.
- `cogs/general.py` — common commands and utilities for users and moderation hooks.
- `cogs/owner.py` — commands restricted to the bot owner (restart, reload, admin tasks).
- `cogs/poller.py` — scheduled tasks, polling, and background checks the bot performs.
- `cogs/template.py` — helpers for loading and rendering message templates.
- `cogs/welcome.py` — welcome flow, verification, ticket creation and approval/deny workflows.

- `cogs/defensie/battles.py` — domain-specific feature (defensie) for battle tracking.
- `cogs/role_selection/roles.py` — role-selection helpers (reaction or command-based role assignment).
- `cogs/standard_messages/` — several files that render and post standard messages (intro, mu_bericht, dreigingsniveau, etc.).

Notes:
- Most cogs expect a single `bot.config` dictionary loaded at startup (from `config.json` or a chosen config). Role and channel IDs should be stored there to avoid hardcoded numeric IDs in multiple places.

## Configuration

- Put your live values into `config.json`. Keys of interest are `roles` and `channels`, both mapping friendly names to numeric Discord IDs.
- `testing_config.json` is provided as an example for a test server — populate it with the test server's role/channel IDs.

Secrets / tokens

- The bot reads the Discord token from an environment variable (the default name is `TOKEN`). The startup CLI lets you override the env var name when running a testing instance.

## Setup

**1. Create a virtual environment and install dependencies**

```bash
python -m venv .venv
source .venv/bin/activate
# Core bot dependencies only:
pip install -e .
# Or include website/API-explorer dependencies too:
pip install -e ".[website]"
```

**2. Create your environment file**

For production, create a `.env` file in the project root:

```
TOKEN=your_production_discord_bot_token
PREFIX=!
INVITE_LINK=https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID
```

For testing, create a `.env_test` file instead:

```
TOKEN_TEST=your_test_discord_bot_token
PREFIX=!
INVITE_LINK=https://discord.com/oauth2/authorize?client_id=YOUR_TEST_CLIENT_ID
```

**3. Create the API keys file**

Add your War Era API key to `_api_keys.json`. Create this file in the project root:

```json
{
    "keys": [
        "your_api_key_here"
    ]
}
```

**4. Configure `config.json` / `testing_config.json`**

Fill in the `roles` and `channels` sections with the numeric Discord IDs from your server.

## Running with Docker Compose

All services are built from the same `Dockerfile`. The compose setup is split into two files:

| File | Purpose | Tracked in git |
|------|---------|----------------|
| `docker-compose.yml` | Production — bot only | ✅ yes |
| `docker-compose.testing.yml` | Testing — bot only (`--testing` flag) | ❌ no |
| `docker-compose.websites.yml` | Testing — website + API explorer | ❌ no |

The website compose files are gitignored because they reference local `.env_test` secrets.

### Production (bot only)

```bash
docker compose build
docker compose up -d
```

### Testing — bot only

```bash
docker compose -f docker-compose.testing.yml build
docker compose -f docker-compose.testing.yml up -d
```

### Testing — Dashboard website + API explorer website

```bash
docker compose -f docker-compose.websites.yml build
docker compose -f docker-compose.websites.yml up -d
```

Services restart automatically (`restart: unless-stopped`).

### View logs

```bash
# Test bot only
docker compose -f docker-compose.testing.yml logs -f discord-bot

# Full testing stack
docker compose -f docker-compose.testing.yml -f docker-compose.websites.yml logs -f

# Individual website services
docker compose -f docker-compose.websites.yml logs -f website
docker compose -f docker-compose.websites.yml logs -f api-explorer
```

### Stop services

```bash
# Test bot only
docker compose -f docker-compose.testing.yml down

# Full testing stack
docker compose -f docker-compose.testing.yml -f docker-compose.websites.yml down
```

---

## Running the bot

Basic (production):

```bash
python bot.py
```

Run with a specific config file:

```bash
python bot.py --config testing_config.json
```

Start a testing instance (uses `testing_config.json` and can load `.env_test`):

```bash
python bot.py --testing
```

Common runtime flags (see `bot.py --help` for full list):

- `--testing` — use testing defaults and the testing config file.
- `--config <path>` — explicitly set the config JSON to load.
- `--token-env NAME` — set the name of the environment variable that holds the Discord token (useful for running two instances concurrently).

Important: the bot will fail fast if the chosen token is not present. For convenience during local testing, place your test token in a `.env_test` file and use `--testing` so the token is loaded automatically.

## Database & backups

- `database/schema.sql` — schema used to create the bot's SQLite database.
- Backups in `database/` are kept as timestamped `.backup` files.

## Development notes

- Follow the pattern in `cogs/` when adding new features: encapsulate logic in a Cog, register it in `bot.py` or allow auto-loading from the `cogs/` folder.
- Avoid hardcoding numeric role/channel IDs in code — add them to `config.json` and reference them from `bot.config`.

## Contributing

Please follow the codebase style, add tests for non-trivial logic, and keep secrets out of commits. If you'd like, I can add an example `CONTRIBUTING.md` with PR/checklist guidelines.

---

If you want a short quick-start script or example `config.json` for a test server, tell me which pieces to include and I will add them.
