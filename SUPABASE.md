# Connecting Supabase, then deploying to App Platform

## 1. Create the Supabase project

1. [supabase.com](https://supabase.com) → sign in → **New project**.
2. Name it anything. **Set a database password and save it somewhere** — it's shown once and you need it in step 2.
3. Pick the region closest to you. Wait ~2 minutes for provisioning.

You do **not** need to create any tables. The bot creates its own schema on first
connect (`videos`, `groups`, `send_log`, `settings`).

## 2. Get the connection string

**Project Settings** (gear icon) → **Database** → **Connection string** → **URI** tab.

You'll see several options. **Choose "Session pooler".** This matters:

| Option | Host | Use it? |
|---|---|---|
| Direct connection | `db.<ref>.supabase.co` | ❌ IPv6-only — fails on App Platform |
| **Session pooler** | `...pooler.supabase.com:5432` | ✅ **this one** |
| Transaction pooler | `...pooler.supabase.com:6543` | works, but session mode suits a long-running bot better |

Copy it and replace `[YOUR-PASSWORD]` with the password from step 1:

```
postgresql://postgres.abcdefghijkl:YourPassword@aws-0-eu-central-1.pooler.supabase.com:5432/postgres
```

If your password contains `@ : / ? # [ ] %`, percent-encode it (`@` → `%40`, `#` → `%23`),
or it'll break URL parsing. Easiest fix is to reset the password to alphanumerics only.

## 3. Run it locally against Supabase

Put the URL in `.env`:

```
DATABASE_URL=postgresql://postgres.abcdefghijkl:YourPassword@aws-0-eu-central-1.pooler.supabase.com:5432/postgres
```

```bash
bash run.sh
```

On success you'll see the usual banner. Check the Supabase dashboard →
**Table Editor** — four tables should now exist. That's your proof the connection works.

> You don't need the `anon` / `service_role` API keys. Those are for the PostgREST
> API. The bot speaks the Postgres wire protocol directly, so the connection string
> is the only credential involved.

## 4. Push to GitHub

```bash
cd ~/Desktop/telegram-video-broadcaster
git init
git add .
git commit -m "Telegram video broadcaster"
git branch -M main
git remote add origin https://github.com/YOUR_USER/telegram-video-broadcaster.git
git push -u origin main
```

`.gitignore` already excludes `.env`, so your token and database password stay local.
**Check that** before pushing: `git status` must not list `.env`.

## 5. Deploy on App Platform

1. DigitalOcean → **Create** → **Apps** → connect GitHub → pick the repo.
2. It detects the `Dockerfile`. On the resource, click **Edit** and change the type
   from *Web Service* to **Worker**. This is the step everyone misses — a long-polling
   bot never opens a port, so a Web Service fails its health check and restarts forever.
3. Instance size: the smallest (`apps-s-1vcpu-0.5gb`, $5/mo) is plenty.
4. **Environment variables** — add these, marking the first two as **encrypted**:

   | Key | Value | Encrypted |
   |---|---|---|
   | `BOT_TOKEN` | your BotFather token | ✅ |
   | `DATABASE_URL` | the session pooler URL | ✅ |
   | `VAULT_CHAT_ID` | `-1004416731612` | |
   | `ADMIN_IDS` | `6651698857` | |
   | `INTERVAL_HOURS` | `4` | |
   | `DELAY_BETWEEN_GROUPS` | `2.0` | |

5. Deploy. Watch the runtime logs for `Running as @yourbot | vault=… | videos=N | groups=N`.

`.do/app.yaml` in the repo holds this same spec if you'd rather import it than click through.

## 6. Stop the local copy

Telegram allows **one poller per token**. Once App Platform is running, kill any local
instance or both will fight and you'll see
`Conflict: terminated by other getUpdates request` in the logs.

```bash
pkill -f bot.py
```

Use the local copy only when the deployed one is paused, and vice versa.

---

## Things worth knowing

**Free-tier pausing.** Supabase pauses free projects after 7 days of *low database
activity*. Your bot queries on every cycle, so it stays active on its own. If you pause
the bot for over a week, the project may sleep — restore it from the dashboard, no data lost.

**Free-tier size.** 500 MB, and you're using well under 1 MB. Two active projects per
free organisation, so this leaves you one for the company website later.

**Backups.** Free tier has no automated backups. Your state is small and mostly
regenerable except the group list — so occasionally run this and keep the output:

```sql
select chat_id, title from groups where active;
```

**Rotating the database password** invalidates `DATABASE_URL`. Update it in both `.env`
and App Platform, then redeploy.
