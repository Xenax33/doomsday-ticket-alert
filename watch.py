#!/usr/bin/env python3
"""Watch Lahore cinema sites for Avengers: Doomsday showtimes and push an alert to your phone.

Sites watched:
  * Cinestar   (cinestar.pk)            - Vista platform, HTML pages
  * Universal  (universalcinemas.com)   - Vista platform, HTML pages (Emporium Mall)
  * Cinepax    (web.cinepax.com)        - JSON API (Packages Mall, Amanah Mall)

Alerts go through ntfy (https://ntfy.sh): install the ntfy app on your phone and
subscribe to the topic written in config.json. No account needed.

Usage:
  python watch.py              run one check (what the scheduled task does)
  python watch.py --loop 120   keep checking every 120 seconds
  python watch.py --dry-run    check and print, send nothing, save nothing
  python watch.py --test       send a test notification to your phone
  python watch.py --reset      forget past alerts (they will fire again)

Only the standard library is used, so there is nothing to install.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.json"
STATE_FILE = HERE / "state.json"
LOG_FILE = HERE / "watch.log"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
MOVIE = "doomsday"  # matched case-insensitively against film titles

# Vista-based sites. known_ids are the movie pages that exist today; the listing
# pages are also scanned each run in case the cinema re-lists the film under a new ID.
VISTA_SITES = [
    {"name": "Cinestar", "base": "https://cinestar.pk/Browsing", "known_ids": ["f-AVENDOO002"]},
    {"name": "Universal", "base": "https://www.universalcinemas.com/browsing", "known_ids": ["m-HO00001519"]},
]
CINEPAX = "https://web.cinepax.com"
CINEPAX_LAHORE = {"0000000014": "Cinepax Packages Mall", "0000000012": "Cinepax Amanah Mall"}

# A cinema name containing any of these counts as Lahore (urgent alert).
# Shows anywhere else still alert, at a lower priority, as an early warning.
LAHORE_WORDS = ("LAHORE", "TOWNSHIP", "XINHUA", "DHA RAYA", "PACKAGES", "AMANAH", "EMPORIUM")

FAILURE_ALERT_AFTER = 15  # consecutive failed runs of one site before telling you


# --------------------------------------------------------------------------- utils

def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            LOG_FILE.write_text("", encoding="utf-8")
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def fetch(url: str, tries: int = 2) -> tuple[str, str]:
    """Return (final_url, body). Raises after `tries` failed attempts."""
    last: Exception | None = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.geturl(), r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            if attempt + 1 < tries:
                time.sleep(3)
    raise RuntimeError(f"{url}: {last}")


def fetch_json(url: str):
    return json.loads(fetch(url)[1])


def is_lahore(cinema: str) -> bool:
    up = cinema.upper()
    return any(w in up for w in LAHORE_WORDS)


def abs_url(href: str) -> str:
    href = html.unescape(href)
    return "https:" + href if href.startswith("//") else href


# ------------------------------------------------------------------------- checks
# Each check returns (findings, anomalies).
#   finding = {"key", "site", "cinema", "film", "sessions", "url"}  -> tickets are live
#   anomaly = {"key", "text"}  -> page looks different from expected; worth a look

def check_vista(site: dict) -> tuple[list, list]:
    findings, anomalies = [], []
    ids = dict.fromkeys(site["known_ids"])
    for listing in ("Movies/NowShowing", "Movies/ComingSoon"):
        try:
            _, page = fetch(f"{site['base']}/{listing}")
        except RuntimeError as e:
            log(f"{site['name']}: listing failed: {e}")
            continue
        for mid, title in re.findall(r'Movies/Details/([^"]+)"\s*>\s*<h3 class="item-title">\s*([^<]+)', page):
            if MOVIE in title.lower():
                ids.setdefault(mid)

    for mid in ids:
        url = f"{site['base']}/Movies/Details/{mid}"
        final_url, page = fetch(url)
        if "/General/Error" in final_url:
            anomalies.append({"key": f"{site['name']}|gone|{mid}",
                              "text": f"{site['name']}: movie page {mid} no longer exists. "
                                      f"It may have been re-listed - check {site['base']}/Movies/NowShowing"})
            continue
        m = re.search(r'<h3 class="boxout-title">\s*([^<]+)', page)
        film = html.unescape(m.group(1)).strip() if m else mid

        # Only count real links; the page's JS also contains the booking URL as a template.
        total = len(re.findall(r'href="[^"]*visSelectTickets\.aspx', page))
        per_cinema = 0
        for block in page.split('<div class="film-item')[1:]:
            links = re.findall(r'href="([^"]*visSelectTickets\.aspx[^"]*)"', block)
            if not links:
                continue
            cm = re.search(r'<h3 class="film-title">\s*([^<]+)', block)
            cinema = html.unescape(cm.group(1)).strip() if cm else "unknown cinema"
            per_cinema += len(links)
            findings.append({"key": f"{site['name']}|{mid}|{cinema}", "site": site["name"],
                             "cinema": cinema, "film": film, "sessions": len(links),
                             "url": abs_url(links[0])})
        if total and not per_cinema:  # markup changed but booking links exist
            findings.append({"key": f"{site['name']}|{mid}|any", "site": site["name"],
                             "cinema": "see page", "film": film, "sessions": total, "url": url})
        if not total and "no sessions scheduled" not in page.lower():
            anomalies.append({"key": f"{site['name']}|odd|{mid}",
                              "text": f"{site['name']}: {film} page changed but shows no booking links. "
                                      f"Have a look: {url}"})
    return findings, anomalies


def count_cinepax_sessions(data: dict) -> int:
    n = 0
    days = data.get("days") or []
    # "days" covers the whole booking window; fall back to top-level "screens" if absent.
    screen_maps = [m for d in days for m in (d.get("screens") or {}, d.get("lateNightScreens") or {})]
    if not days:
        screen_maps = [data.get("screens") or {}]
    for screens in screen_maps:
        for screen in screens.values():
            n += len(screen.get("sessions") or screen.get("times") or [])
    return n


def check_cinepax() -> tuple[list, list]:
    findings, anomalies = [], []
    films: dict[str, dict] = {}
    for ep in ("advance-booking", "movies", "upcoming"):
        data = fetch_json(f"{CINEPAX}/api/{ep}")
        items = data if isinstance(data, list) else next((v for v in data.values() if isinstance(v, list)), [])
        for f in items:
            if MOVIE in str(f.get("title", "")).lower():
                films.setdefault(f["filmCode"], f)
    if not films:
        anomalies.append({"key": "Cinepax|nofilm",
                          "text": "Cinepax no longer lists Avengers: Doomsday anywhere - check web.cinepax.com"})

    for code, f in films.items():
        details = f"{CINEPAX}/pages/movie-details.html?" + urllib.parse.urlencode({"filmCode": code, "movie": f["title"]})
        lahore_hit = False
        for cid, cname in CINEPAX_LAHORE.items():
            n = count_cinepax_sessions(fetch_json(f"{CINEPAX}/api/showtimes?filmCode={code}&cinemaId={cid}"))
            if n:
                lahore_hit = True
                findings.append({"key": f"Cinepax|{code}|{cid}", "site": "Cinepax", "cinema": cname,
                                 "film": f["title"], "sessions": n, "url": details})
        if f.get("hasFutureSessions") and not lahore_hit:
            findings.append({"key": f"Cinepax|{code}|somewhere", "site": "Cinepax",
                             "cinema": "a Cinepax branch (not Lahore yet)", "film": f["title"],
                             "sessions": 0, "url": details})
    return findings, anomalies


# --------------------------------------------------------------------------- ntfy

def ensure_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    cfg = {"ntfy_server": "https://ntfy.sh",
           "ntfy_topic": "doomsday-lhr-" + secrets.token_urlsafe(9).replace("_", "x").replace("-", "y").lower()}
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    log(f"Created config.json. Subscribe to topic '{cfg['ntfy_topic']}' in the ntfy app.")
    return cfg


def notify(cfg: dict, title: str, body: str, priority: int, tags: str, click: str | None = None) -> None:
    headers = {"Title": title.encode("ascii", "ignore").decode(), "Priority": str(priority), "Tags": tags}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(f"{cfg['ntfy_server'].rstrip('/')}/{cfg['ntfy_topic']}",
                                 data=body.encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


# --------------------------------------------------------------------------- main

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"alerted": {}, "failures": {}}


def run_once(cfg: dict, dry_run: bool) -> int:
    state = load_state()
    alerted, failures = state.setdefault("alerted", {}), state.setdefault("failures", {})
    checks = [(s["name"], lambda s=s: check_vista(s)) for s in VISTA_SITES] + [("Cinepax", check_cinepax)]
    new_findings, new_anomalies, summary = [], [], []

    for name, check in checks:
        try:
            findings, anomalies = check()
        except Exception as e:  # one broken site must not stop the others
            failures[name] = failures.get(name, 0) + 1
            log(f"{name}: FAILED ({failures[name]} in a row): {e}")
            if failures[name] == FAILURE_ALERT_AFTER:
                new_anomalies.append({"key": f"{name}|down|{time.time():.0f}",
                                      "text": f"Watcher can't reach {name} ({FAILURE_ALERT_AFTER} failed checks "
                                              f"in a row). Check it by hand until this clears. Last error: {e}"})
            summary.append(f"{name}: error")
            continue
        failures[name] = 0
        summary.append(f"{name}: {sum(f['sessions'] for f in findings)} shows" if findings else f"{name}: none")
        seen = {f["key"] for f in new_findings} | {a["key"] for a in new_anomalies}
        for item in findings + anomalies:
            if item["key"] not in alerted and item["key"] not in seen:
                seen.add(item["key"])
                (new_findings if "site" in item else new_anomalies).append(item)

    log("Checked - " + ", ".join(summary))

    for f in sorted(new_findings, key=lambda f: not is_lahore(f["cinema"])):
        lahore = is_lahore(f["cinema"])
        title = f"TICKETS LIVE: {f['film']} @ {f['cinema']}" if lahore else f"Doomsday booking opened: {f['site']}"
        body = (f"{f['site']} - {f['cinema']}\n{f['film']}\n"
                + (f"{f['sessions']} shows on sale. " if f["sessions"] else "")
                + "Tap to book now.")
        log(f"ALERT {'(Lahore)' if lahore else '(elsewhere)'}: {f['site']} / {f['cinema']} / {f['film']} -> {f['url']}")
        if not dry_run:
            notify(cfg, title, body, priority=5 if lahore else 4,
                   tags="rotating_light,ticket" if lahore else "ticket", click=f["url"])
            alerted[f["key"]] = datetime.now().isoformat(timespec="seconds")

    for a in new_anomalies:
        log(f"NOTICE: {a['text']}")
        if not dry_run:
            notify(cfg, "Doomsday watcher: needs a look", a["text"], priority=3, tags="warning")
            alerted[a["key"]] = datetime.now().isoformat(timespec="seconds")

    if not dry_run:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return len(new_findings)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loop", type=int, metavar="SECONDS", help="keep running, checking every SECONDS")
    ap.add_argument("--dry-run", action="store_true", help="check and print only; send nothing, save nothing")
    ap.add_argument("--test", action="store_true", help="send a test notification and exit")
    ap.add_argument("--reset", action="store_true", help="forget which alerts were already sent")
    args = ap.parse_args()

    cfg = ensure_config()
    if args.reset and STATE_FILE.exists():
        STATE_FILE.unlink()
        log("State cleared.")
    if args.test:
        notify(cfg, "Doomsday watcher test", "If you can read this, alerts will reach your phone.",
               priority=5, tags="white_check_mark", click=f"{VISTA_SITES[0]['base']}/Movies/ComingSoon")
        log(f"Test notification sent to topic {cfg['ntfy_topic']}.")
        return

    while True:
        try:
            run_once(cfg, args.dry_run)
        except Exception as e:  # e.g. ntfy unreachable; keep the loop alive
            log(f"Run failed: {e}")
            if not args.loop:
                sys.exit(1)
        if not args.loop:
            return
        time.sleep(max(30, args.loop))


if __name__ == "__main__":
    main()
