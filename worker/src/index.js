/**
 * Cloudflare Worker version of watch.py.
 *
 * Runs every minute (cron in wrangler.toml), checks Lahore cinemas for
 * Avengers: Doomsday showtimes, and pushes an alert through ntfy.
 *
 *   env.NTFY_TOPIC   secret  - your ntfy topic (set with `wrangler secret put NTFY_TOPIC`)
 *   env.NTFY_SERVER  var     - defaults to https://ntfy.sh
 *   env.STATE        KV      - remembers which alerts were already sent
 *
 * GET /check on the worker URL runs a dry check (no alerts, nothing saved) and
 * returns JSON, so you can see that the cinema sites answer Cloudflare's servers.
 * GET /check?movie=<title> does the same for another film, to test detection.
 */

const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36";
const MOVIE = "doomsday";

const VISTA_SITES = [
  { name: "Cinestar", base: "https://cinestar.pk/Browsing", knownIds: ["f-AVENDOO002"] },
  { name: "Universal", base: "https://www.universalcinemas.com/browsing", knownIds: ["m-HO00001519"] },
];
const CINEPAX = "https://web.cinepax.com";
const CINEPAX_LAHORE = { "0000000014": "Cinepax Packages Mall", "0000000012": "Cinepax Amanah Mall" };
const LAHORE_WORDS = ["LAHORE", "TOWNSHIP", "XINHUA", "DHA RAYA", "PACKAGES", "AMANAH", "EMPORIUM"];
const DOWN_ALERT_AFTER_MS = 30 * 60 * 1000;

// ------------------------------------------------------------------ helpers

async function fetchText(url, tries = 2) {
  let last;
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(url, { headers: { "User-Agent": UA, "Accept-Language": "en" }, redirect: "follow" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return { finalUrl: r.url, body: await r.text() };
    } catch (e) {
      last = e;
    }
  }
  throw new Error(`${url}: ${last?.message ?? last}`);
}

const fetchJson = async (url) => JSON.parse((await fetchText(url)).body);

const unescape = (s) =>
  s.replace(/&amp;/g, "&").replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&lt;/g, "<").replace(/&gt;/g, ">");

const absUrl = (href) => {
  href = unescape(href);
  return href.startsWith("//") ? "https:" + href : href;
};

const isLahore = (cinema) => LAHORE_WORDS.some((w) => cinema.toUpperCase().includes(w));

// ------------------------------------------------------------------- checks
// Each check returns { findings, anomalies }.
//   finding = { key, site, cinema, film, sessions, url }  -> tickets are live
//   anomaly = { key, text }                                -> page looks different; worth a look

async function checkVista(site, movie) {
  const findings = [];
  const anomalies = [];
  const ids = new Set(movie === MOVIE ? site.knownIds : []);

  for (const listing of ["Movies/NowShowing", "Movies/ComingSoon"]) {
    try {
      const { body } = await fetchText(`${site.base}/${listing}`);
      if (!body.toLowerCase().includes(movie)) continue;
      for (const m of body.matchAll(/Movies\/Details\/([^"]+)"\s*>\s*<h3 class="item-title">\s*([^<]+)/g)) {
        if (m[2].toLowerCase().includes(movie)) ids.add(m[1]);
      }
    } catch (e) {
      console.log(`${site.name}: listing failed: ${e.message}`);
    }
  }

  for (const id of ids) {
    const url = `${site.base}/Movies/Details/${id}`;
    const { finalUrl, body } = await fetchText(url);
    if (finalUrl.includes("/General/Error")) {
      anomalies.push({
        key: `${site.name}|gone|${id}`,
        text: `${site.name}: movie page ${id} no longer exists. It may have been re-listed - check ${site.base}/Movies/NowShowing`,
      });
      continue;
    }
    const filmMatch = body.match(/<h3 class="boxout-title">\s*([^<]+)/);
    const film = filmMatch ? unescape(filmMatch[1]).trim() : id;

    // Only count real links; the page's JS also contains the booking URL as a template.
    const total = (body.match(/href="[^"]*visSelectTickets\.aspx/g) || []).length;
    let perCinema = 0;
    for (const block of body.split('<div class="film-item').slice(1)) {
      const links = [...block.matchAll(/href="([^"]*visSelectTickets\.aspx[^"]*)"/g)].map((m) => m[1]);
      if (!links.length) continue;
      const cm = block.match(/<h3 class="film-title">\s*([^<]+)/);
      const cinema = cm ? unescape(cm[1]).trim() : "unknown cinema";
      perCinema += links.length;
      findings.push({
        key: `${site.name}|${id}|${cinema}`, site: site.name, cinema, film, sessions: links.length, url: absUrl(links[0]),
      });
    }
    if (total && !perCinema) {
      findings.push({ key: `${site.name}|${id}|any`, site: site.name, cinema: "see page", film, sessions: total, url });
    }
    if (!total && !body.toLowerCase().includes("no sessions scheduled")) {
      anomalies.push({
        key: `${site.name}|odd|${id}`,
        text: `${site.name}: ${film} page changed but shows no booking links. Have a look: ${url}`,
      });
    }
  }
  return { findings, anomalies };
}

function countCinepaxSessions(data) {
  const days = data.days || [];
  // "days" covers the whole booking window; fall back to top-level "screens" if absent.
  const maps = days.length
    ? days.flatMap((d) => [d.screens || {}, d.lateNightScreens || {}])
    : [data.screens || {}];
  let n = 0;
  for (const screens of maps) {
    for (const s of Object.values(screens)) n += (s.sessions || s.times || []).length;
  }
  return n;
}

async function checkCinepax(movie) {
  const findings = [];
  const anomalies = [];
  const films = new Map();
  for (const ep of ["advance-booking", "movies", "upcoming"]) {
    const data = await fetchJson(`${CINEPAX}/api/${ep}`);
    const items = Array.isArray(data) ? data : Object.values(data).find(Array.isArray) || [];
    for (const f of items) {
      if (String(f.title || "").toLowerCase().includes(movie) && !films.has(f.filmCode)) films.set(f.filmCode, f);
    }
  }
  if (!films.size) {
    anomalies.push({ key: "Cinepax|nofilm", text: "Cinepax no longer lists Avengers: Doomsday anywhere - check web.cinepax.com" });
  }

  for (const [code, f] of films) {
    const details = `${CINEPAX}/pages/movie-details.html?` + new URLSearchParams({ filmCode: code, movie: f.title });
    let lahoreHit = false;
    const counts = await Promise.all(
      Object.keys(CINEPAX_LAHORE).map((cid) =>
        fetchJson(`${CINEPAX}/api/showtimes?filmCode=${code}&cinemaId=${cid}`).then(countCinepaxSessions),
      ),
    );
    Object.entries(CINEPAX_LAHORE).forEach(([cid, cname], i) => {
      if (!counts[i]) return;
      lahoreHit = true;
      findings.push({ key: `Cinepax|${code}|${cid}`, site: "Cinepax", cinema: cname, film: f.title, sessions: counts[i], url: details });
    });
    if (f.hasFutureSessions && !lahoreHit) {
      findings.push({
        key: `Cinepax|${code}|somewhere`, site: "Cinepax", cinema: "a Cinepax branch (not Lahore yet)",
        film: f.title, sessions: 0, url: details,
      });
    }
  }
  return { findings, anomalies };
}

// --------------------------------------------------------------------- ntfy

async function notify(env, title, body, priority, tags, click) {
  const headers = { Title: title.replace(/[^\x20-\x7e]/g, ""), Priority: String(priority), Tags: tags };
  if (click) headers.Click = click;
  const server = (env.NTFY_SERVER || "https://ntfy.sh").replace(/\/$/, "");
  const r = await fetch(`${server}/${env.NTFY_TOPIC}`, { method: "POST", headers, body });
  if (!r.ok) throw new Error(`ntfy HTTP ${r.status}`);
}

// --------------------------------------------------------------------- main
// KV state (free plan allows 1000 writes/day, so we write only when something changes):
//   "alerted"     -> { key: isoTime }    alerts already sent
//   "down_since"  -> { site: epochMs }   when each site started failing

async function runCheck(env, { dryRun, movie = MOVIE }) {
  const checks = [
    ...VISTA_SITES.map((s) => [s.name, () => checkVista(s, movie)]),
    ["Cinepax", () => checkCinepax(movie)],
  ];
  const results = await Promise.allSettled(checks.map(([, fn]) => fn()));

  const alerted = dryRun ? {} : (await env.STATE.get("alerted", "json")) || {};
  const downSince = dryRun ? {} : (await env.STATE.get("down_since", "json")) || {};
  let alertedChanged = false;
  let downChanged = false;

  const summary = {};
  const newFindings = [];
  const newAnomalies = [];
  const seen = new Set();
  const now = Date.now();

  results.forEach((res, i) => {
    const name = checks[i][0];
    if (res.status === "rejected") {
      summary[name] = `error: ${res.reason?.message ?? res.reason}`;
      if (!downSince[name]) {
        downSince[name] = now;
        downChanged = true;
      } else if (now - downSince[name] > DOWN_ALERT_AFTER_MS) {
        newAnomalies.push({
          key: `${name}|down|${downSince[name]}`,
          text: `Watcher can't reach ${name} for over 30 min. Check it by hand until this clears. Last error: ${summary[name]}`,
        });
      }
      return;
    }
    if (downSince[name]) {
      delete downSince[name];
      downChanged = true;
    }
    const { findings, anomalies } = res.value;
    summary[name] = findings.length ? `${findings.reduce((a, f) => a + f.sessions, 0)} shows` : "none";
    for (const item of [...findings, ...anomalies]) {
      if (alerted[item.key] || seen.has(item.key)) continue;
      seen.add(item.key);
      (item.site ? newFindings : newAnomalies).push(item);
    }
  });

  newFindings.sort((a, b) => isLahore(b.cinema) - isLahore(a.cinema));
  const sent = [];
  for (const f of newFindings) {
    const lahore = isLahore(f.cinema);
    if (!dryRun) {
      await notify(
        env,
        lahore ? `TICKETS LIVE: ${f.film} @ ${f.cinema}` : `Doomsday booking opened: ${f.site}`,
        `${f.site} - ${f.cinema}\n${f.film}\n${f.sessions ? `${f.sessions} shows on sale. ` : ""}Tap to book now.`,
        lahore ? 5 : 4,
        lahore ? "rotating_light,ticket" : "ticket",
        f.url,
      );
      alerted[f.key] = new Date().toISOString();
      alertedChanged = true;
    }
    sent.push({ ...f, lahore });
  }
  for (const a of newAnomalies) {
    if (!dryRun) {
      await notify(env, "Doomsday watcher: needs a look", a.text, 3, "warning");
      alerted[a.key] = new Date().toISOString();
      alertedChanged = true;
    }
    sent.push(a);
  }

  if (alertedChanged) await env.STATE.put("alerted", JSON.stringify(alerted));
  if (downChanged) await env.STATE.put("down_since", JSON.stringify(downSince));

  console.log(`Checked - ${Object.entries(summary).map(([k, v]) => `${k}: ${v}`).join(", ")}`);
  const result = { checkedAt: new Date().toISOString(), dryRun, summary, alerts: sent };
  // Heartbeat every 5 min (288 writes/day, under the free 1000). Read it with:
  //   npx wrangler kv key get last_run --binding STATE --remote
  if (!dryRun && new Date().getUTCMinutes() % 5 === 0) await env.STATE.put("last_run", JSON.stringify(result));
  return result;
}

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(runCheck(env, { dryRun: false }));
  },

  async fetch(request, env) {
    const { pathname, searchParams } = new URL(request.url);
    if (pathname === "/check") {
      // ?movie=<title words> lets you test detection on a film that is on sale now.
      const movie = (searchParams.get("movie") || MOVIE).toLowerCase().slice(0, 40);
      const result = await runCheck(env, { dryRun: true, movie });
      return Response.json(result, { headers: { "Cache-Control": "no-store" } });
    }
    return new Response("Doomsday ticket watcher. GET /check runs a dry check.\n");
  },
};
