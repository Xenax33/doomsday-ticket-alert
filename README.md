# Doomsday ticket alert

Checks Lahore cinemas for **Avengers: Doomsday** showtimes and pushes a loud alert to your phone the
moment booking opens. Tapping the alert opens the booking page.

| Chain | Branches treated as Lahore | How it's checked |
|---|---|---|
| Cinestar | Township, Xinhua Mall, DHA Raya | movie page on cinestar.pk |
| Universal | Emporium Mall | movie page on universalcinemas.com |
| Cinepax | Packages Mall, Amanah Mall | Cinepax JSON API |

Lahore shows send an **urgent** alert. Shows in other cities (e.g. Cinestar Sargodha) send a
normal-priority early warning, since Lahore usually follows within hours.
If a page changes shape, or a site stays unreachable for ~30 min, you get a "needs a look" notice
instead of silence. Each alert fires once.

There are two ways to run it:

- **`worker/`: Cloudflare Worker** (recommended). Runs in the cloud every minute, so your PC can be off.
- **`watch.py`: Python on your PC.** Every 2 min via Task Scheduler, only while the PC is on.

## 1. Phone (needed for both)

1. Install **ntfy** (Play Store / App Store), tap **+**, and subscribe to a topic name of your own.
   Make it long and random, e.g. `doomsday-lhr-k3x9q7...`. Topics are public: anyone who knows the
   name can read your alerts, so never commit it. `config.json` and `worker/.dev.vars` are git-ignored.
2. In the topic's settings, allow it to override Do Not Disturb so it rings at night.

## 2a. Cloudflare Worker

```powershell
cd worker
npx wrangler login                              # opens the browser; sign in to Cloudflare
npx wrangler kv namespace create STATE          # paste the printed id into wrangler.toml
npx wrangler secret put NTFY_TOPIC              # paste your ntfy topic when asked
npx wrangler deploy
```

If deploy says you need a workers.dev subdomain, open the Workers section of the Cloudflare
dashboard once (that creates it) and deploy again. The Worker has no public URL; the cron runs it.

**Is it running?** Every 5 minutes it saves its last result:

```powershell
npx wrangler kv key get last_run --binding STATE --remote
```

**Test on Cloudflare's network:** `npx wrangler dev --remote`, then open
`http://localhost:8787/check` (dry check, sends nothing) or `/check?movie=godzilla` to test
detection against a film that's on sale now.

Local testing: put `NTFY_TOPIC=<topic>` in `worker/.dev.vars`, then
`npx wrangler dev --test-scheduled` and open `http://localhost:8787/check`.

## 2b. Python on your PC

```powershell
python watch.py --test       # creates config.json with a random topic, sends a test alert
python watch.py --dry-run    # one check, prints only
powershell -ExecutionPolicy Bypass -File register_task.ps1   # every 2 min in the background
```

Or `python watch.py --loop 120` in a terminal you leave open. `watch.log` records each check;
`python watch.py --reset` re-arms alerts that already fired.
