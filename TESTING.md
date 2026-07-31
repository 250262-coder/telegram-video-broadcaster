# Testing the bot, start to finish

Work through this in order. Each stage proves one thing; if a stage fails, the
next one can't tell you anything useful.

---

## 0. Install

```bash
cd ~/Desktop/telegram-video-broadcaster
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Put your **BOT_TOKEN** and **DATABASE_URL** in `.env` for now
(see [SUPABASE.md](SUPABASE.md) for the connection string). Leave `ADMIN_IDS` and
`VAULT_CHAT_ID` at their placeholder values — the bot detects those and starts in
**setup mode** instead of trying to post into a fake chat.

In [@BotFather](https://t.me/BotFather): `/setprivacy` → **Disable**. Without this
the bot can't see commands in groups.

---

## 1. Get your IDs

```bash
python bot.py
```

You should see a `SETUP MODE` banner in the logs with your bot's @username.

Open a DM with the bot and send **`/id`**. It replies with:

```
IDs
This chat: 123456789 (private)
You: 123456789
Still missing in .env: ADMIN_IDS, VAULT_CHAT_ID
```

In a private chat, "This chat" and "You" are the same number. **That's your
`ADMIN_IDS` value.**

Now the vault channel:

1. Create a new **private channel** in Telegram.
2. Add your bot as an **administrator** with "Post messages".
3. Post anything in the channel, then **forward that post to the bot in DM**.

The bot replies with `Forwarded from: -1001234567890 — Your Channel Name`.
**That's your `VAULT_CHAT_ID`.**

Paste both into `.env`, stop the bot (Ctrl-C), start it again. The banner should
now read `Running as @yourbot | vault=-100… | interval=4.0h | videos=0 | groups=0`.

> Forwarding works even for channels the bot isn't in, so you can use this trick
> to check any chat's id later.

---

## 2. Prove the vault is wired up

Post a **video** into the vault channel.

- Expected: a DM from the bot — `➕ Added video to rotation (msg 4). Queue size: 1.`
- Send `/videos` — the video should be listed.

**If nothing happens**, check the logs. A line like
`Ignoring video from chat -100999 (Other Channel) — configured vault is -100888`
means `VAULT_CHAT_ID` points at the wrong channel. If there's no log line at all,
the bot isn't an admin in that channel — the Bot API sends it no updates.

Post 2–3 more videos so rotation has something to rotate.

---

## 3. Add a test group

Create a throwaway group, add the bot to it. Expected: a DM —
`✅ Added to Test Group (-100…). Now targeting 1 group(s).`

If you added the bot before starting it, send `/here` inside the group instead.

Confirm with `/groups`.

> Telegram silently upgrades basic groups to supergroups, which **changes the
> chat id**. The bot follows the migration automatically, so you may see the id
> in `/groups` change once. That's expected, not a bug.

---

## 4. Send one on demand

```
/sendnow
```

The video should land in the test group within a second or two, and the bot
replies `Broadcast video #1: 1 sent`.

Then check the important part — **rotation**. Run `/sendnow` three more times and
watch which video arrives each time. It should cycle through all of them before
repeating any.

`/status` should now show a real "Last run" timestamp.

---

## 5. Prove the schedule fires

Don't wait four hours. Shrink the interval:

```
/interval 0.05
```

That's three minutes. `/status` shows the next run time. Wait it out — a video
should appear in the group unprompted, and you get a summary DM.

Then set it back: `/interval 4`.

Also test the brake:

```
/pause     → wait past the next run, nothing should be sent
/resume
```

---

## 6. Prove restarts are safe

With the bot running, note "Next run" from `/status`. Ctrl-C, restart, check
`/status` again — the next run should be roughly the same moment, not reset to
now. The schedule is anchored to the last run stored in Postgres, so a restart
(or a server reboot) doesn't cause a double-send or a skipped cycle.

Your videos and groups should still be listed. If they vanished, `DATABASE_URL` points
at a different project than before.

---

## 7. Break it on purpose

Worth doing once, so you recognise the behaviour in production:

| Do this | Expected |
|---|---|
| Remove the bot from the test group, then `/sendnow` | Group auto-deactivates, you get a DM, `/groups` drops it |
| Delete a video from the vault channel, then `/sendnow` until it comes up | That entry is removed from rotation, cycle continues |
| Restrict "Send media" for members in the group, then `/sendnow` | Send fails and is logged; grant the bot admin to fix |

Check the audit trail afterwards:

```bash
psql "$DATABASE_URL" -c 'select sent_at, video_id, chat_id, status, detail from send_log order by id desc limit 20'
```

---

## 8. Before going live

- [ ] `/interval` set to what you actually want (3–5h)
- [ ] `DELAY_BETWEEN_GROUPS` at 2.0 or higher — raise it as group count grows
- [ ] Real videos in the vault, checked with `/videos`
- [ ] Every real group added and visible in `/groups`
- [ ] Bot is admin in groups that use slow mode (admins bypass it; members don't)
- [ ] `.env` is **not** committed to git (`.gitignore` covers it) — check with `git status`
- [ ] Running under systemd or Docker with restart-on-failure, not in a terminal

First live day: leave `/status` handy and check that "Last run" keeps advancing.
