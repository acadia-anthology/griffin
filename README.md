# Griffin

Discord bot for Jazz's book club server — the Goblin Gold economy, book/sprint tracking, and related community features. Built separately from Abraxos and the Archives bot; shares patterns, not code or identity.

## Local setup

1. `python -m venv venv && source venv/bin/activate`
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN` and `DATABASE_URL`
4. `python bot.py`

## Deploy

Hosted on Railway. Needs a Postgres plugin attached (provides `DATABASE_URL` automatically) and `DISCORD_TOKEN` set as an environment variable.
