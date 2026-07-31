# Telegram marketing video broadcaster

Copies posts from a private "vault" channel into every group the bot is in, on a fixed interval (default every 4h, changeable with one command). Any content type works — text, photos, videos, documents, albums. Rotates through the whole pool so groups don't see the same post twice in a row.

**No files are stored anywhere.** Telegram holds them; this bot stores only the `message_id` of each post inside your vault channel and uses `copyMessage` to repost it. No S3, no disk, no re-uploading, no bandwidth cost.

## How it works

```
you post anything ──▶ private vault channel ──▶ bot records message_id in Postgres
                                                        │
                                    every N hours, pick least-recently-sent
                                                        │
                                    copyMessage ──▶ group 1, group 2, group 3 …
                                                    (2s apart, backs off on 429)
```

## Setup

**1. Create the bot**

Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
Then `/setprivacy` → **Disable** — otherwise the bot can't see commands in groups.

**2. Create the vault channel**

- New Telegram channel, **Private**. Name it anything ("Video Vault").
- Add your bot as an **administrator** with "Post messages" permission.

**3. Create the database**

State lives in Postgres, not on disk — see [SUPABASE.md](SUPABASE.md) for the
walkthrough. Short version: create a free Supabase project and copy the
**Session pooler** connection string into `DATABASE_URL`.

**4. Configure and run**

```bash
cp .env.example .env      # add BOT_TOKEN and DATABASE_URL; leave the other two as-is
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

**5. Get your IDs from the bot itself**

With the placeholders untouched the bot starts in **setup mode**. DM it:

- **`/id`** → your user id, for `ADMIN_IDS`
- **forward any post from the vault channel to it** → the channel id, for `VAULT_CHAT_ID`

Paste both into `.env` and restart. Full walkthrough in [TESTING.md](TESTING.md).

**6. Load videos and groups**

- Post into the vault channel — text, photos, videos, documents, albums all work. Each post gets registered automatically and you get a DM confirmation. (Upload from your Telegram app, not through the bot — that way you're not capped at the 50MB bot upload limit; the vault accepts up to 2GB per file.)
- Add the bot to each target group. It registers itself on join. If it was already in a group before the bot ran, send `/here` in that group.
- First broadcast fires 2 minutes after a cold start, then every N hours.

> Videos posted to the vault *before* the bot became admin can't be picked up — the Bot API can't read channel history. Re-post them, or forward them into the channel again.

## Commands (admins only, in DM with the bot)

| Command | What it does |
|---|---|
| `/status` | Counts, interval, next run, what's up next |
| `/posts` | The rotation queue, next first (alias `/videos`) |
| `/remove 3` | Drop post #3 from rotation; `/restore 3` undoes it, `/restore` lists removed |
| `/groups` | Active target groups |
| `/sendnow` | Broadcast immediately; `/sendnow 12` for a specific post |
| `/interval 3` | Change cadence to every 3 hours (decimals fine: `0.5`) |
| `/pause` / `/resume` | Stop and restart scheduled broadcasts |
| `/here` | Register the group you send it in |
| `/id` | Show this chat's id, your id, and the origin of anything forwarded to the bot |

## Rate limits — why the delays are there

Telegram allows roughly 30 messages/second overall and ~20 messages/minute into a single chat, and its anti-spam system reacts badly to a bot posting into dozens of groups at once. The bot therefore:

- waits `DELAY_BETWEEN_GROUPS` (default 2s) between groups — 50 groups ≈ 100s per cycle
- catches `429 Too Many Requests` and sleeps exactly the `retry_after` Telegram asks for, then retries
- retries transient network/5xx errors up to 3 times with backoff
- deactivates a group automatically when the bot is kicked or the chat is deleted, and DMs you about it

If you push into 100+ groups, raise the delay rather than lowering it. Getting the bot limited is much more expensive than a slow cycle.

## Deploy

State lives in Postgres, so the container is disposable. Full walkthrough in
[SUPABASE.md](SUPABASE.md).

### DigitalOcean App Platform (~$5/mo)

1. Push the repo to GitHub (`.env` is gitignored — verify with `git status`).
2. Create → Apps → pick the repo. It detects the `Dockerfile`.
3. **Change the component type from Web Service to Worker.** This is the step
   everyone misses: a long-polling bot never binds a port, so a Web Service fails
   its health check and restarts forever. Workers aren't health-checked.
4. Add `BOT_TOKEN` and `DATABASE_URL` as **encrypted** env vars, plus
   `VAULT_CHAT_ID` and `ADMIN_IDS` as plain ones.
5. Deploy, then watch the runtime logs for the `Running as @yourbot` banner.

`.do/app.yaml` holds this spec if you'd rather import it than click through.

### VPS + systemd

Any small box works. Long polling means **no domain and no TLS certificate needed**.

```bash
sudo adduser --system --group --home /opt/videobot botuser
sudo -u botuser git clone <your-repo> /opt/videobot
cd /opt/videobot
sudo -u botuser python3 -m venv .venv
sudo -u botuser .venv/bin/pip install -r requirements.txt
sudo -u botuser cp .env.example .env && sudo -u botuser nano .env

sudo cp deploy/videobot.service /etc/systemd/system/
sudo systemctl enable --now videobot
journalctl -u videobot -f
```

### Docker

```bash
cp .env.example .env && nano .env
docker compose up -d --build
docker compose logs -f
```

### What not to use

Vercel, Netlify, and Lambda are wrong for this — you need a process that stays alive
to hold the scheduler and the polling loop, and serverless functions get frozen
between requests.

**Only ever run one instance.** Telegram allows a single poller per token; a second
one produces `Conflict: terminated by other getUpdates request` in both logs.

## Notes

- `CAPTION_SUFFIX` appends a CTA to every caption. It rewrites the caption, so bold/links in the *original* caption lose their formatting — put HTML in the suffix itself, or leave the setting blank to copy captions untouched.
- Every content type Telegram can copy is registered: text, photos, videos, GIFs, documents, audio, voice, stickers, polls, locations. Service messages, invoices, giveaways and stories are skipped because `copyMessage` refuses them.
- **Albums** (multi-photo/video posts) count as one entry and are reposted grouped, via `copyMessages`. Removing one removes the whole album.
- `CAPTION_SUFFIX` is skipped for albums and for types without a caption field (text, polls, stickers) — Telegram rejects a caption on those.
- Deleting a video from the vault channel: the bot drops it from rotation the first time a copy fails with "message to copy not found".
- `send_log` keeps a row per (video, group, outcome). Handy for proving delivery: `psql "$DATABASE_URL" -c 'select * from send_log order by id desc limit 20'`.

## Files

```
bot.py            entrypoint: DI wiring, polling, scheduler start
config.py         .env parsing and validation
db.py             Postgres schema + queries via asyncpg (posts, groups, send_log, settings)
check_db.py       one-shot DATABASE_URL verifier with plain-English errors
tests/run_tests.py offline suite: SQL syntax + behaviour, no server needed
broadcaster.py    the fan-out loop: copy_message, delays, retries, backoff
handlers.py       commands, vault ingestion, group join/leave tracking
scheduling.py     APScheduler interval job, restart-safe next-run calculation
SUPABASE.md       database + App Platform deployment walkthrough
.do/app.yaml      App Platform spec (worker, not web service)
```
