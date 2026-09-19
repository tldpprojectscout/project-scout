/**
 * Project Scout — Cloudflare Worker (Telegram webhook) + the shared render/gate module used by discord.js.
 *
 * The Workers make NO live GitHub / GitLab / Devpost / MLH calls. Their only data source is published.json, a
 * snapshot written by the GitHub Actions publisher after every repo has passed the screening gate. Anything not
 * in that file — or whose screening record has expired, or that is older than the freshness window — is never shown.
 * If the snapshot is missing, degraded, or stale (no screening in 36 h) every repo lane replies with PAUSE_MSG.
 *
 * Telegram commands: /quant /fintech /accounting /swe /cyber /data /pm /marketing [keywords] · /oss <major> · /research <major>
 *                    /orgs · /basics <major> · /path <major> · /cases <major> · /hackathons · /courses · /domains
 * Secrets: TELEGRAM_BOT_TOKEN, WEBHOOK_SECRET, OWNER_CHAT_ID (lock to one chat; remove to open to a channel).
 */
export const PUBLISHED_URL = "https://raw.githubusercontent.com/tldpprojectscout/project-scout/main/published.json";
export const PAUSE_MSG = "Recommendations are paused: automated screening hasn't completed recently. TLDP staff have been notified.";
export const DEFAULT_LABEL = "Automated checks completed; not a safety guarantee.";
export const EVERGREEN_HEAD = "Evergreen reference (not a fresh repo)";
const STALE_MS = 36 * 3600e3;
const EXPECTED_POLICY = 2;   // published.json must be produced under this screening policy version, or it is rejected
const MAX = 6;
const DISCORD_LIMIT = 2000, TELEGRAM_LIMIT = 4096;

export const MAJORS = {
  quant: { label: "📈 Quant" }, fintech: { label: "💳 Finance / FinTech" }, swe: { label: "💻 Software Engineering" },
  cyber: { label: "🔐 Cybersecurity" }, data: { label: "📊 Data Analytics" }, pm: { label: "📋 Project Management" },
  accounting: { label: "🧾 Accounting" },
  marketing: { label: "📣 Digital Marketing" },
};
const ALIAS = { acct: "accounting", accountancy: "accounting", bookkeeping: "accounting", audit: "accounting", tax: "accounting", finance: "fintech", software: "swe", security: "cyber", analytics: "data", project: "pm", projectmanagement: "pm", digitalmarketing: "marketing", seo: "marketing" };
export const LANES = { build: { label: "🧪 Build this" }, oss: { label: "🤝 Contribute" }, research: { label: "🔬 Research" } };
export const OTHER_LANES = ["orgs", "start", "hackathons", "cases", "path", "hf", "projects"];
const LANE_ALIAS = { contribute: "oss", opensource: "oss", paper: "research", papers: "research", new: "build",
  org: "orgs", nonprofit: "orgs", volunteer: "orgs", mission: "orgs",
  basics: "start", starter: "start", starters: "start", learn: "start", ideas: "start", begin: "start",
  hackathon: "hackathons", hack: "hackathons", hacks: "hackathons", events: "hackathons",
  case: "cases", casestudy: "cases", casestudies: "cases", path: "path", paths: "path", roadmap: "path", year: "path", plan: "path",
  huggingface: "hf", hugging: "hf", models: "hf", datasets: "hf", dataset: "hf", spaces: "hf", ai: "hf",
  project: "projects", ladder: "projects", python: "projects", sql: "projects" };
// First keyword → the feed sub-label it selects (keys are "major|lane|sublabel", written by the publisher).
const SWE_LABEL = { python: "Python backend", backend: "Python backend", sql: "SQL", frontend: "Frontend", go: "Other languages" };
const DATA_LABEL = { powerbi: "Power BI", power: "Power BI", tableau: "Tableau" };
export const COURSES = {
  quant: { mth9814: "MTH 9814 Financial Markets & Securities", mth9815: "MTH 9815 Software Engineering for Finance", mth9816: "MTH 9816 Fundamentals of Trading",
    mth9821: "MTH 9821 Numerical Methods for Finance", mth9831: "MTH 9831 Probability & Stochastic Processes", mth9842: "MTH 9842 Optimization Techniques in Finance",
    mth9845: "MTH 9845 Market & Credit Risk Management", mth9855: "MTH 9855 Asset Allocation & Portfolio Management", mth9863: "MTH 9863 Volatility Filtering & Estimation",
    mth9866: "MTH 9866 FX Modeling & Market Making", mth9878: "MTH 9873 / 9878 Interest Rate Models", mth9875: "MTH 9875 The Volatility Surface",
    mth9876: "MTH 9876 Credit Risk Models", mth9879: "MTH 9879 Market Microstructure Models", mth9882: "MTH 9882 Fixed Income Risk Management",
    mth9887: "MTH 9887 Blockchain Technologies in Finance", mth9893: "MTH 9893 / 9867 Time Series & Algorithmic Trading",
    mth9894: "MTH 9894 / 9897 Algorithmic & Systematic Trading", mth9896: "MTH 9896 Behavioral Finance", mth9899: "MTH 9898 / 9899 Data Science & Machine Learning in Finance" },
  fintech: { fin3000: "FIN 3000 Principles of Finance", fin3610: "FIN 3610 Corporate Finance", fin3710: "FIN 3710 Investment Analysis",
    modeling: "Financial modeling & valuation", statements: "Financial statement analysis", options: "Derivatives & options", bonds: "Fixed income",
    fx: "International finance & FX", markets: "Financial markets & trading", personal: "Personal finance & fintech apps", risk: "Risk management & credit", realestate: "Real estate finance" },
  accounting: { ledger: "Financial accounting & the ledger", statements: "Financial statement analysis", xbrl: "SEC filings & XBRL", cost: "Managerial & cost accounting",
    audit: "Auditing & internal controls", tax: "Tax", ais: "Accounting information systems", fraud: "Forensic accounting & fraud", excel: "Excel & spreadsheet automation" },
  pm: { scrum: "Scrum", jira: "Jira", confluence: "Confluence", kanban: "Kanban", metrics: "Agile metrics & reporting", roadmap: "Roadmaps & OKRs",
    stories: "Requirements & user stories", risk: "Risk, stakeholders & schedules" },
};
export const CYBER_DOMAINS = {
  d1: "D1 Security & Risk Management (GRC)", d2: "D2 Asset Security", d3: "D3 Security Architecture & Engineering", d4: "D4 Communication & Network Security",
  d5: "D5 Identity & Access Management", d6: "D6 Security Assessment & Testing", d7: "D7 Security Operations", d8: "D8 Software Development Security",
};
export const ORGS = {
  nonprofit: { label: "🌱 Non-profit", orgs: { mozilla: "Mozilla", OWASP: "OWASP", wikimedia: "Wikimedia", EFForg: "EFF", datakind: "DataKind", ushahidi: "Ushahidi",
    hackforla: "Hack for LA", codeforamerica: "Code for America", torproject: "Tor Project", freeCodeCamp: "freeCodeCamp", BetaNYC: "BetaNYC (NYC civic tech)" } },
  public: { label: "🏛 Public sector", orgs: { cisagov: "CISA", GSA: "US GSA", "18F": "18F", usds: "US Digital Service", nasa: "NASA", usnistgov: "NIST", CDCgov: "CDC",
    CityOfNewYork: "City of New York", NYCPlanning: "NYC Dept of City Planning", alphagov: "UK GDS" } },
  private: { label: "🏢 Private sector", orgs: { microsoft: "Microsoft", google: "Google", aws: "AWS", IBM: "IBM", cloudflare: "Cloudflare", elastic: "Elastic",
    goldmansachs: "Goldman Sachs", "man-group": "Man Group", jpmorganchase: "JPMorgan Chase", bloomberg: "Bloomberg" } },
};
const ORG_SECTOR = {};
for (const { label, orgs } of Object.values(ORGS)) for (const [o, n] of Object.entries(orgs)) ORG_SECTOR[o.toLowerCase()] = { label, name: n };
export const HACK_LINKS = [
  ["MLH season calendar", "https://mlh.io/seasons/2027/events"],
  ["Devpost in-person", "https://devpost.com/hackathons?challenge_type[]=in-person&order_by=deadline&search=new+york"],
  ["Eventbrite NYC", "https://www.eventbrite.com/d/ny--new-york/hackathon/"],
  ["Meetup NYC", "https://www.meetup.com/find/?keywords=hackathon&location=us--ny--New%20York&source=EVENTS"],
  ["Luma NYC", "https://lu.ma/nyc?q=hackathon"],
];

// ---- links & escaping ---------------------------------------------------------------------------------------------
export const ALLOWED_LINK_HOSTS = ["github.com", "gitlab.com", "huggingface.co", "devpost.com", "mlh.io", "arxiv.org", "www.kaggle.com", "kaggle.com",
  "www.makeovermonday.co.uk", "www.workout-wednesday.com", "www.opencasestudies.org", "thedfirreport.com", "hacktoberfest.com", "openpracticelibrary.com",
  "guides.18f.gov", "www.agilealliance.org", "www.quantconnect.com", "overthewire.org", "play.picoctf.org", "tryhackme.com", "www.freecodecamp.org",
  "scrumguides.org", "www.atlassian.com", "learndigital.withgoogle.com", "academy.hubspot.com", "ctftime.org", "www.pmi.org", "www.linkedin.com",
  "www.eventbrite.com", "www.meetup.com", "lu.ma", "www.cuny.edu", "www.spaceappschallenge.org", "beta.nyc", "opendata.cityofnewyork.us", "www.nist.gov",
  "www.cisa.gov", "owasp.org", "wiki.wireshark.org", "pages.nist.gov", "attack.mitre.org"];
export function safeUrl(u) {
  try { const x = new URL(String(u)); return x.protocol === "https:" && ALLOWED_LINK_HOSTS.includes(x.hostname) && !x.username && !x.password ? x.href : null; }
  catch { return null; }
}
export const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
// Discord markdown escape for third-party / user text: neutralises masked links [x](url), bold, code, quotes, mentions.
export const mdEsc = (s) => String(s).replace(/[\\*_~`|<>\[\]()]/g, (c) => "\\" + c).replace(/@/g, "@​");
// A link only when the destination is https and on the allowlist; otherwise escaped text with no link at all.
export function link(text, url, md) {
  const u = safeUrl(url);
  if (!u) return md ? mdEsc(text) : esc(text);
  return md ? `[${mdEsc(text)}](<${u}>)` : `<a href="${u}">${esc(text)}</a>`;
}
const GFI = "/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22";

// ---- published.json + the render-time gate --------------------------------------------------------------------------
export async function loadPublished() {
  const cache = globalThis.caches?.default, key = new Request(PUBLISHED_URL, { method: "GET" });
  let r = cache ? await cache.match(key) : null;
  if (!r) {
    r = await fetch(PUBLISHED_URL, { headers: { "user-agent": "project-scout-bot" } });
    if (!r.ok) return null;
    r = new Response(await r.text(), { headers: { "content-type": "application/json", "cache-control": "s-maxage=1800" } });
    if (cache) await cache.put(key, r.clone());
  }
  try { return await r.json(); } catch { return null; }
}
export function operational(pub, now = Date.now()) {
  const m = pub?.meta;
  if (!m || typeof m !== "object") return false;
  if (m.policy_version !== EXPECTED_POLICY) return false;   // incompatible snapshot: pause rather than mis-render
  if (m.status !== "operational") return false;
  if (!Array.isArray(pub?.feed) && (typeof pub?.feed !== "object" || pub?.feed === null)) return false;  // malformed
  const t = Date.parse(m.last_screening_at || "");
  return Number.isFinite(t) && now - t <= STALE_MS;
}
export function eligible(row, meta, now = Date.now()) {
  if (!row || typeof row.full_name !== "string" || !row.screened) return false;
  if (!/^[0-9a-f]{40}$/.test(row.screened.sha || "")) return false;
  const exp = Date.parse(row.screened.expires || "");
  if (!Number.isFinite(exp) || exp <= now) return false;
  if (row.kind === "evergreen") return true;
  const days = Number(meta?.fresh_days) || 30, field = meta?.fresh_field === "created_at" ? "created_at" : "pushed_at";
  const t = Date.parse(row[field] || "");
  return Number.isFinite(t) && Math.floor((now - t) / 864e5) <= days;  // dates are YYYY-MM-DD: count whole days, 30 passes, 31 fails
}
const label = (pub) => pub?.meta?.label || DEFAULT_LABEL;
const dedupe = (rows) => { const s = new Set(); return rows.filter((r) => r && !s.has(r.full_name) && s.add(r.full_name)); };
const tokensOf = (extra) => (extra || "").toLowerCase().split(/\s+/).filter((t) => t && !/^(advanced|any|all)$/.test(t));
const matches = (r, tokens) => tokens.every((t) => `${r.full_name} ${r.description || ""}`.toLowerCase().includes(t));

// Rows for a feed lane: key selection by major/lane/sub-label, keyword filter, then the gate. No fallback of any kind.
export function fromPublished(pub, lane, major, extra, now = Date.now()) {
  if (!pub?.feed) return [];
  const tokens = tokensOf(extra);
  let keys = Object.keys(pub.feed).filter((k) => (lane === "orgs" ? k.startsWith("orgs|") : (!major || k.startsWith(`${major}|`)) && k.includes(`|${lane}|`)));
  if (lane === "build" && !tokens.length && phase(new Date(now))[0] < 2) {
    const bk = keys.filter((k) => /\|build\|(learn|tutorial|starter)$/.test(k));
    if (bk.length) keys = bk;
  }
  const first = tokens[0];
  const sub = first && ((major === "swe" && SWE_LABEL[first]) || (major === "data" && DATA_LABEL[first]) ||
    (major === "cyber" && CYBER_DOMAINS[first]) || COURSES[major]?.[first]);
  if (sub) {
    keys = keys.filter((k) => k.toLowerCase().includes(sub.toLowerCase()));
    tokens.shift();
  }
  let rows = dedupe(keys.flatMap((k) => pub.feed[k] || []));
  if (tokens.length) rows = rows.filter((r) => matches(r, tokens));
  return rows.filter((r) => eligible(r, pub.meta, now));
}
// Undergraduate level selection. BEGINNER is the default; a student opts into intermediate/challenge. We never
// substitute a HARDER level than asked (only a simpler one), so "no beginner repos" shows nothing harder, not a
// challenge project. Levels come from levels.py (multi-signal), carried on each row as `level`.
export const LEVEL_LABEL = { beginner: "🟢 Beginner", intermediate: "🟡 Intermediate", challenge: "🟠 Undergraduate Challenge" };
export function wantedLevel(extraRaw) {
  const x = (extraRaw || "").toLowerCase();
  if (/\b(advanced|hard|challenge)\b/.test(x)) return "challenge";
  if (/\b(intermediate|medium)\b/.test(x)) return "intermediate";
  return "beginner";  // default
}
export function byLevel(rows, extraRaw) {
  const want = wantedLevel(extraRaw);
  const lvOf = (it) => it.level || "beginner";   // a row without a level is treated as the default (beginner)
  const order = { beginner: ["beginner"], intermediate: ["intermediate", "beginner"], challenge: ["challenge", "intermediate", "beginner"] }[want];
  for (const lv of order) {                 // take the requested level; fall back only to SIMPLER levels, never harder
    const r = rows.filter((it) => lvOf(it) === lv);
    if (r.length) return { level: lv, rows: r };
  }
  return { level: want, rows: [] };
}
export function lookup(pub, lane, major, extraRaw, now = Date.now()) {
  const rows = fromPublished(pub, lane, major, stripLevel(extraRaw), now);
  if (lane === "orgs") return rows.sort((a, b) => (a.pushed_at < b.pushed_at ? 1 : -1)).slice(0, MAX + 3);
  const picked = byLevel(rows, extraRaw).rows;          // beginner default; explicit choice for harder
  return ordered(picked, ceilingFor(extraRaw, now)).slice(0, MAX + 3);
}
export function startItems(pub, major, now = Date.now()) {
  const st = pub?.evergreen?.starters || {};
  const rows = major ? st[major] || [] : dedupe(Object.values(st).flat());
  return ordered(rows.filter((r) => eligible(r, pub.meta, now))).slice(0, major ? 11 : 14);
}
export function domainItems(pub, d, now = Date.now()) {
  const x = pub?.evergreen?.cyber_domains?.[d];
  return x ? { ...x, repos: ordered((x.repos || []).filter((r) => eligible(r, pub.meta, now))) } : null;
}
export function caseItems(pub, major, now = Date.now()) {
  const cs = pub?.evergreen?.cases;
  if (!cs) return null;
  const list = [...(cs[major] || []), ...(cs.all || [])].map((c) => ({ ...c, repo: c.repo && eligible(c.repo, pub.meta, now) ? c.repo : null }));
  return list.length ? list : null;
}
export function pathFor(pub, major, now = Date.now()) {
  const p = pub?.evergreen?.paths?.[major];
  if (!p) return null;
  return { stages: pub.evergreen.stages || [], path: p.map((stage) => stage.map((s) => ({ ...s, repo: s.repo && eligible(s.repo, pub.meta, now) ? s.repo : null }))) };
}
export const hackathons = (pub) => (Array.isArray(pub?.hackathons?.items) ? pub.hackathons.items : []);

// ---- ordering ---------------------------------------------------------------------------------------------------------
const RANK = { "🟢 starter": 0, "🟡 intermediate": 1, "🔴 advanced": 2 };
export function phase(d = new Date()) {
  const m = d.getMonth() + 1;
  return [9, 10].includes(m) ? [0, "Phase 1 · Sep–Oct · starter"] : [11, 12, 1].includes(m) ? [1, "Phase 2 · Nov–Jan · up to intermediate"] : [2, "Phase 3 · Feb–May · all levels"];
}
export function difficulty(it) {
  const size = it.size || 0, stars = it.stargazers_count || 0;
  if (size < 5000 && stars < 500) return "🟢 starter";
  if (size < 50000 && stars < 5000) return "🟡 intermediate";
  return "🔴 advanced";
}
export function ordered(items, ceiling = null) {
  const ranked = [...items].sort((a, b) => RANK[difficulty(a)] - RANK[difficulty(b)] || (b.stargazers_count || 0) - (a.stargazers_count || 0));
  if (ceiling === null) return ranked;
  return ranked.filter((it) => RANK[difficulty(it)] <= ceiling).concat(ranked.filter((it) => RANK[difficulty(it)] > ceiling));
}
const ceilingFor = (extra, now) => (/\b(advanced|hard|challenge|intermediate|medium|any|all)\b/i.test(extra || "") ? null : phase(new Date(now))[0]);
export function levelFilter(items, extra) {
  const x = extra || "";
  if (/\b(beginner|easy|starter)\b/i.test(x)) { const r = items.filter((it) => RANK[difficulty(it)] === 0); return r.length ? r : items; }
  if (/\b(intermediate|medium)\b/i.test(x)) { const r = items.filter((it) => RANK[difficulty(it)] === 1); return r.length ? r : items.filter((it) => RANK[difficulty(it)] <= 1); }
  if (/\b(advanced|hard|challenge)\b/i.test(x)) { const r = items.filter((it) => RANK[difficulty(it)] >= 1).reverse(); return r.length ? r : items.slice().reverse(); }
  return items;
}
const stripLevel = (extra) => (extra || "").replace(/\b(beginner|easy|starter|intermediate|medium|advanced|hard|challenge|any|all)\b/gi, "").trim();

// ---- rendering: head + whole blocks + tail, trimmed to the platform limit by dropping blocks from the end -----------
export function pack(head, blocks, tail, limit) {
  const out = [head];
  let len = head.length + tail.length + 2;
  for (const b of blocks) {
    if (len + b.length + 1 > limit) break;
    out.push(b); len += b.length + 1;
  }
  if (out.length === 1 && blocks.length) out.push(md(head) ? "*(list too long for one message)*" : "<i>(list too long for one message)</i>");
  return `${out.join("\n")}\n\n${tail}`;
}
const md = (s) => /\*\*/.test(s);  // heads are bold: ** in Discord markdown, <b> in Telegram HTML
export const italic = (s, isMd) => (isMd ? `*${s}*` : `<i>${s}</i>`);
export const bold = (s, isMd) => (isMd ? `**${s}**` : `<b>${s}</b>`);
const limitFor = (isMd) => (isMd ? DISCORD_LIMIT : TELEGRAM_LIMIT);
const noteLine = (pub, isMd) => italic(esc0(label(pub), isMd), isMd);
const esc0 = (s, isMd) => (isMd ? mdEsc(s) : esc(s));

export function renderRepo(it, lane, isMd) {
  const e = (s) => esc0(s, isMd);
  let d = (it.description || "").trim();
  if (d.length > 140) d = d.slice(0, 140) + "…";
  const lang = it.language ? ` · ${e(it.language)}` : "";
  const tail = lane === "oss" ? `\n  👉 ${link("open good-first-issues", it.html_url + GFI, isMd)}` : "";
  const repoLink = it.commit_url || it.html_url;  // reviewed commit when screened, else the repo default branch
  const lvl = LEVEL_LABEL[it.level];
  const lm = it.level_meta || {};
  const lvlLine = lvl ? `\n  🎓 ${lvl} · ${e(lm.effort || "")}\n  Prereqs: ${e(lm.prereqs || "")}\n  Task: ${e(lm.task || "")}\n  Done when: ${e(lm.success || "")}` : "";
  return `• ${link(it.full_name, repoLink, isMd)} ⭐${Number(it.stargazers_count) || 0}${lang} · ${lvl || difficulty(it)}\n  ${e(d) || "(no description)"}${lvlLine}${tail}`;
}
export function render(head, lane, items, pub, isMd = false, evergreen = false) {
  if (!items.length) return `${head}\nNothing matched. Try fewer keywords.`;
  head += "\n" + italic(evergreen ? EVERGREEN_HEAD : `📶 ${phase()[1]} · easiest first`, isMd);
  return pack(head, items.map((it) => renderRepo(it, lane, isMd)), noteLine(pub, isMd), limitFor(isMd));
}
export function renderOrgs(head, items, pub, isMd = false) {
  if (!items.length) return `${head}\nNothing matched. Try fewer keywords.`;
  const e = (s) => esc0(s, isMd);
  const blocks = items.map((it) => {
    const [org, repo = ""] = it.full_name.split("/");
    const s = ORG_SECTOR[org.toLowerCase()] || { label: "🏢", name: org };
    return renderRepo(it, "oss", isMd) + `\n  🏷 ${s.label} · ${e(s.name)} · ${link("LinkedIn", "https://www.linkedin.com/search/results/companies/?keywords=" + encodeURIComponent(s.name), isMd)}` +
      `\n  📝 Resume: Open-Source Contributor, ${e(s.name)} (${e(repo)})`;
  });
  return pack(head, blocks, noteLine(pub, isMd), limitFor(isMd));
}
export function renderCases(head, items, pub, isMd = false) {
  const e = (s) => esc0(s, isMd);
  const blocks = items.map(({ target, blurb, repo }) => repo
    ? `• ${link(repo.full_name, repo.html_url, isMd)} ⭐${Number(repo.stargazers_count) || 0} · ${Number(repo.open_issues_count) || 0} open issues · ${difficulty(repo)}\n  ${e(blurb)} · ${link("good first issues", repo.html_url + GFI, isMd)}`
    : `• ${link(String(target).replace("https://", "").replace(/\/$/, ""), target, isMd)}\n  ${e(blurb)}`);
  head += "\n" + italic(EVERGREEN_HEAD, isMd);
  return pack(head, blocks, italic("Pick one, read its CONTRIBUTING file, claim an issue. Your own case goes in the TLDP library.", isMd) + "\n" + noteLine(pub, isMd), limitFor(isMd));
}
export function renderPath(major, data, pub, isMd = false) {
  const e = (s) => esc0(s, isMd);
  const cur = Math.min(phase()[0], 3);
  const head = bold(`🗺 ${MAJORS[major].label} — your year, in order`, isMd) + "\n" + italic(`${EVERGREEN_HEAD}. Start at Stage 1 even if it feels easy. ▶ = now (${phase()[1]}).`, isMd);
  const stages = [...data.stages, ["🔥", "any time", "Further along? Challenge track — skip ahead or take one on the side"]];
  const blocks = [];
  stages.forEach(([num, months, title], i) => {
    if (!data.path[i]) return;
    const lines = [bold(i < data.stages.length ? `${i === cur ? "▶" : "•"} Stage ${num} · ${months} · ${e(title)}` : `${num} ${e(title)}`, isMd)];
    for (const { target, todo, repo } of data.path[i]) {
      const isRepo = String(target).includes("/") && !String(target).startsWith("http");
      const name = repo ? repo.full_name : String(target).replace("https://", "").replace(/\/$/, "");
      const url = repo ? repo.html_url : isRepo ? `https://github.com/${target}` : target;
      lines.push(`  ${link(name, url, isMd)}${repo ? ` ⭐${Number(repo.stargazers_count) || 0}` : ""}\n    ${e(todo)}`);
    }
    blocks.push("\n" + lines.join("\n"));
  });
  return pack(head, blocks, noteLine(pub, isMd), limitFor(isMd));
}
// ---- Hugging Face lane (published by hf.py) -------------------------------------------------------------------------
// These rows are NOT repositories: no clone, no code scan. Their own record (passing screen, 40-hex pinned SHA, not
// expired) is re-checked here before anything renders, and the note under the head never claims a code scan.
export const HF_NOTE = "link-screened metadata + README, not code-scanned — read the Files tab before you run anything";
export function hfEligible(row, now = Date.now()) {
  if (!row || typeof row.full_name !== "string" || !row.full_name.startsWith("hf:")) return false;
  const s = row.screened;
  if (!s || s.result !== "pass" || !/^[0-9a-f]{40}$/.test(s.sha || "")) return false;
  const exp = Date.parse(s.expires || "");
  return Number.isFinite(exp) && exp > now;
}
export function hfItems(pub, major, extra, now = Date.now()) {
  const all = pub?.huggingface?.items || {};
  const rows = major ? all[major] || [] : dedupe(Object.values(all).flat());
  const tokens = tokensOf(extra);
  return rows.filter((r) => hfEligible(r, now) && matches(r, tokens)).slice(0, MAX + 3);
}
export function renderHF(head, items, pub, isMd = false) {
  const e = (s) => esc0(s, isMd);
  if (!items.length) return `${head}\nNothing matched. Try fewer keywords, or another major.`;
  head += "\n" + italic(HF_NOTE, isMd);
  const blocks = items.map((r) => {
    const meta = [r.industry ? `${r.industry_emoji || ""} ${r.industry}`.trim() : "", r.hf_kind, r.task, r.license]
      .filter(Boolean).map(e).join(" · ");
    const warn = (r.warnings || []).slice(0, 2).map((w) => `\n  ⚠️ ${e(w)}`).join("");
    const prereq = r.prereq ? `\n  🧰 Need first: ${e(r.prereq)}` : "";
    const steps = (r.steps || []).slice(0, 3).map((s, i) => `\n  ${i + 1}. ${e(s)}`).join("");
    const done = r.done ? `\n  ✅ Done when ${e(r.done)}.` : "";
    return `• ${link(r.id, r.html_url, isMd)} ❤${Number(r.likes) || 0} · ${meta}\n  ${e(r.description || "(no description)")}${warn}${prereq}${steps}${done}`;
  });
  return pack(head, blocks, noteLine(pub, isMd), limitFor(isMd));
}
// ---- the curated Python + SQL ladder (evergreen.projects) ------------------------------------------------------------
export const LEVELS3 = { start: "🟢 Start here", build: "🟡 Build on it", deep: "🔴 Go deep" };
export function projectItems(pub, major, now = Date.now()) {
  const all = pub?.evergreen?.projects || {};
  const rows = major ? all[major] || [] : Object.values(all).flat();
  // the repo must still pass the gate; an item whose repo was dropped is not shown (its task names that repo)
  return rows.filter((it) => it && it.repo && eligible(it.repo, pub.meta, now));
}
export function renderProjects(head, items, pub, isMd = false) {
  const e = (s) => esc0(s, isMd);
  if (!items.length) return `${head}
The project ladder is not published yet — try again after the next feed run.`;
  head += "\n" + italic("Work down the list. Each one names what YOU build.", isMd);
  const order = { start: 0, build: 1, deep: 2 };
  const blocks = [...items].sort((a, b) => (order[a.level] ?? 9) - (order[b.level] ?? 9)).map((it) =>
    `• ${LEVELS3[it.level] || ""} · ${e(it.lang || "")} — ${link(it.repo.full_name, it.repo.commit_url || it.repo.html_url, isMd)} ⭐${Number(it.repo.stargazers_count) || 0}
  🛠 ${e(it.todo || "")}`);
  return pack(head, blocks, noteLine(pub, isMd), limitFor(isMd));
}
export function renderHacks(head, items, isMd = false) {
  const e = (s) => esc0(s, isMd);
  const more = HACK_LINKS.map(([n, u]) => link(n, u, isMd)).join(" · ");
  if (!items.length) return `${head}\nNothing listed right now. Check: ${more}`;
  const blocks = items.slice(0, 10).map((h) =>
    `• ${link(h.title, h.url, isMd)}${h.prize ? ` · 🏆 ${e(h.prize)}` : ""}\n  📅 ${e(h.when)} · 📍 ${e(h.where)}${h.org && h.org !== h.src ? ` · ${e(h.org)}` : ""} · via ${e(h.src)}`);
  return pack(head, blocks, `🔎 More: ${more}`, limitFor(isMd));
}
export function renderDomain(pub, d, isMd = false) {
  const x = domainItems(pub, d);
  if (!x) return null;
  const head = bold(`🔐 ${esc0(x.label, isMd)}`, isMd) + "\n" + italic(EVERGREEN_HEAD, isMd);
  const ref = x.ref ? `📖 Reference: ${link(x.ref[0], x.ref[1], isMd)}\n` : "";
  return pack(head, x.repos.map((r) => renderRepo(r, "oss", isMd)), ref + noteLine(pub, isMd), limitFor(isMd));
}
export function coursesHelp(major) {
  const m = COURSES[major] ? major : null;
  if (!m) return "<b>🎓 Course-aligned searches</b>\n/courses quant · /courses fintech · /courses accounting · /courses pm";
  const cmd = { quant: "/quant", fintech: "/fintech", accounting: "/accounting", pm: "/pm" }[m];
  return `<b>🎓 ${MAJORS[m].label} — course codes you can search</b>\n` + Object.entries(COURSES[m]).map(([k, name]) => `${cmd} ${k} — ${name}`).join("\n") +
    `\nAdd keywords after the code: <code>${cmd} ${Object.keys(COURSES[m])[0]} python</code>`;
}
export const DOMAINS_HELP = "<b>🔐 Cybersecurity — the 8 CISSP domains</b>\n" + Object.entries(CYBER_DOMAINS).map(([k, l]) => `/cyber ${k} — ${l}`).join("\n") +
  "\nAdd keywords: <code>/cyber d7 sigma</code>. Curated projects per domain: <code>/domains d7</code>.";

// ---- parsing ----------------------------------------------------------------------------------------------------------
// "/oss cyber honeypot" -> {lane:"oss", major:"cyber", extra:"honeypot"}; "nba stats" -> {lane:"build", major:null, extra:"nba stats"}
export function parse(text) {
  const words = String(text).trim().slice(0, 200).replace(/^\/(\w+)@\w+/, "$1").replace(/^\//, "").split(/\s+/);
  let lane = words[0].toLowerCase();
  lane = LANES[lane] || OTHER_LANES.includes(lane) ? lane : LANE_ALIAS[lane];
  if (lane) words.shift(); else lane = "build";
  let major = (words[0] || "").toLowerCase();
  major = MAJORS[major] ? major : ALIAS[major] || null;
  const extra = (major ? words.slice(1) : words).join(" ").trim().slice(0, 60);
  return { lane, major, extra };
}

// One answer for every lane, shared by both bots. Returns the message text; never throws user-visible internals.
export function answerFor(pub, lane, major, extra, isMd, now = Date.now()) {
  if (!operational(pub, now)) return PAUSE_MSG;
  const b = (s) => bold(s, isMd);
  if (lane === "hackathons") return renderHacks(b("🏁 NYC in-person hackathons (Devpost + MLH, refreshed by the feed)"), hackathons(pub), isMd);
  if (lane === "projects") {
    const head = b(`🐍 Python & SQL projects · ${major ? MAJORS[major].label : "🔎 All majors"}`);
    return renderProjects(head, projectItems(pub, major, now), pub, isMd);
  }
  if (lane === "hf") {
    const head = b(`🤗 Hugging Face · ${major ? MAJORS[major].label : "🔎 All majors"}`) + (extra ? ` · ${italic(esc0(extra, isMd), isMd)}` : "");
    return renderHF(head, hfItems(pub, major, extra, now), pub, isMd);
  }
  if (lane === "path") {
    const data = pathFor(pub, major || "swe", now);
    return data ? renderPath(major || "swe", data, pub, isMd) : "The learning path isn't published yet — try again after the next feed run.";
  }
  if (lane === "cases") {
    const items = caseItems(pub, major || "swe", now);
    return items ? renderCases(b(`📁 Case studies you can contribute to · ${MAJORS[major || "swe"].label}`), items, pub, isMd) : "Case studies aren't published yet — try again after the next feed run.";
  }
  const laneLabel = { orgs: "🤝 Mission-driven orgs", start: "📚 Start here — ideas & basics" }[lane] || LANES[lane].label;
  const head = b(`${laneLabel} · ${major ? MAJORS[major].label : "🔎 All majors"}`) + (extra ? ` · ${italic(esc0(extra, isMd), isMd)}` : "");
  if (lane === "orgs") return renderOrgs(head, lookup(pub, "orgs", null, extra, now), pub, isMd);
  if (lane === "start") return render(head, "build", startItems(pub, major, now), pub, isMd, true);
  return render(head, lane, lookup(pub, lane, major, extra, now), pub, isMd);
}

// ---- Telegram -----------------------------------------------------------------------------------------------------------
const HELP =
  "<b>Project Scout</b> — GitHub project ideas by major, pre-screened.\n\n" +
  "<b>New here? Start with /path &lt;major&gt;</b> — your whole year in order, stage by stage.\n" +
  "/quant · /fintech · /accounting · /swe · /cyber · /data · /pm · /marketing — fresh repos to build\n" +
  "  /swe sql · /swe frontend · /data powerbi · /data tableau · /cyber d1…d8 (/domains lists them)\n" +
  "  /accounting xbrl · /accounting audit · /accounting tax — accounting topics (/courses accounting lists them)\n" +
  "  /quant mth9821 · /fintech fin3710 · /pm jira — Baruch course-aligned (/courses quant lists codes)\n" +
  "  Add a level: <code>/cyber beginner</code> · <code>/data intermediate</code> · <code>/swe advanced</code>\n" +
  "/oss &lt;major&gt; — repos with open <i>good first issue</i> tickets\n" +
  "/research &lt;major&gt; — fresh paper code (cites arXiv)\n" +
  "/orgs — non-profit, public-sector and company repos that welcome contributors\n" +
  "/basics &lt;major&gt; — evergreen idea lists, roadmaps, beginner courses\n" +
  "/hackathons — NYC in-person hackathons · /cases &lt;major&gt; — case-study collections\n" +
  "/hf &lt;major&gt; — Hugging Face models, datasets &amp; Spaces to build on (link-screened, not code-scanned)\n" +
  "/projects &lt;major&gt; — the Python &amp; SQL project ladder, easiest first (cyber, swe, data)\n\n" +
  "Add keywords to narrow: <code>/cyber honeypot</code>, <code>/oss data pandas</code>.\n\n" +
  "<i>Everything here comes from a snapshot rebuilt every 6 hours after automated screening. Automated checks completed; not a safety guarantee. " +
  "Never run a project's install scripts before reading them; report anything suspicious to TLDP staff.</i>";

async function tgSend(env, chatId, text) {
  await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text: text.slice(0, TELEGRAM_LIMIT), parse_mode: "HTML", disable_web_page_preview: true }),
  });
}

export async function handleUpdate(env, update) {
  const msg = update?.message || update?.edited_message;
  if (!msg || !msg.chat) return;
  const chatId = String(msg.chat.id);
  if (env.OWNER_CHAT_ID && chatId !== String(env.OWNER_CHAT_ID)) return;
  const t = String(msg.text || "").trim().slice(0, 200);
  if (!t || /^\/?(start|help)$/i.test(t)) return tgSend(env, chatId, HELP);
  const dm = t.match(/^\/?domains(?:\s+(d[1-8]))?$/i);
  if (dm) {
    if (!dm[1]) return tgSend(env, chatId, DOMAINS_HELP);
    const pub = await loadPublished().catch(() => null);
    if (!operational(pub)) return tgSend(env, chatId, PAUSE_MSG);
    return tgSend(env, chatId, renderDomain(pub, dm[1].toLowerCase()) || "That domain isn't published yet.");
  }
  const cm = t.match(/^\/?courses(?:\s+(\w+))?$/i);
  if (cm) return tgSend(env, chatId, coursesHelp(ALIAS[(cm[1] || "").toLowerCase()] || (cm[1] || "").toLowerCase()));
  const { lane, major, extra } = parse(t);
  if (lane === "build" && !major && !extra) return tgSend(env, chatId, HELP);
  let text;
  try { text = answerFor(await loadPublished().catch(() => null), lane, major, extra, false); }
  catch (e) { console.log("answer error", e?.message); text = "Something went wrong on our side. Try again in a minute."; }
  await tgSend(env, chatId, text);
}

export default {
  async fetch(request, env, ctx) {
    if (request.method === "GET") return new Response("Project Scout webhook is up.", { status: 200 });
    if (request.method !== "POST") return new Response("method not allowed", { status: 405 });
    if (!env.WEBHOOK_SECRET || request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 401 });  // fail closed: no secret configured = nothing is accepted
    }
    let update;
    try { update = await request.json(); } catch { return new Response("bad request", { status: 400 }); }
    ctx.waitUntil(handleUpdate(env, update).catch((e) => console.log("handle error", e?.message)));
    return new Response("ok", { status: 200 });
  },
};
