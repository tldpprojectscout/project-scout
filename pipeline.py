#!/usr/bin/env python3
"""
pipeline.py — the two halves of the one gate, wired to the feed.

  discover(out, full=False)   untrusted stage: search GitHub, screen every candidate + curated repo through gate.py
                              (clone + Semgrep + deep vet + link screen), write build/{index,screening,published,drop}.json.
                              Touches untrusted repos; runs with a no-permission GitHub token and NO Discord/Telegram
                              secrets. Sends nothing.
  publish(inp, index_only)    offline stage: re-validate every row against build/screening.json via gate.eligible (no
                              network, no clone), render, send to Telegram/Discord, and copy published.json / index.json
                              / screening.json to the repo root (published.json is what the Cloudflare Workers read).

Nothing a student can see skips gate.eligible. If no repo qualifies for a major, a self-contained synthetic project
idea (no external links) is offered instead. Every repo message ends with the "not a safety guarantee" label.
"""
import json
import os
import sys
from datetime import datetime, timezone

import gate
import hf
import levels
import links
import project_scout as ps

MIN_PASSING = 20   # below this many passing records the snapshot is "degraded" and the Workers pause (fail closed)


# ---- self-contained fallbacks (no external links, synthetic data) --------------------------------------------------
SYNTHETIC = {
    "quant": "Build a tiny backtester: generate 2 years of synthetic daily prices with a random walk, code a moving-average crossover, and report the hit rate and max drawdown. No data download — you make the data.",
    "fintech": "Build a loan-amortization calculator: input principal, rate, term; output the monthly payment and a full amortization table. Pure math, no API, no data.",
    "swe": "Build a command-line to-do app that saves tasks to a local JSON file: add, list, complete, delete. No network, no database — just your code and a file.",
    "cyber": "Write a password-strength checker: score a password on length, character classes and a small built-in list of common passwords, and explain the score. All local, no wordlist download.",
    "data": "Generate a synthetic 'store sales' table in code (dates, products, prices, quantities), then compute monthly revenue, the top product and a simple chart. You create the data, so there is nothing to download.",
    "accounting": "Build a double-entry ledger in code: post ten journal entries to a JSON file, then print a trial balance — and a check that fails when debits and credits do not match. Nothing to download, nothing to install.",
    "pm": "Model a 3-sprint project in a JSON file: epics, stories, estimates. Write a script that prints a burndown table. No tools to install beyond your language.",
    "marketing": "Build a UTM-link builder: given a base URL and campaign fields, output correctly-encoded tracking URLs and validate them. Pure string handling, no external service.",
}


NOTE_COOLDOWN_DAYS = 7  # SYNTHETIC is one fixed string per major: without this it reposts verbatim every run


def _note_recent(seen: dict, key: str) -> bool:
    """True if this major's synthetic idea was already posted inside the cooldown."""
    last = seen.get("note:" + key)
    return bool(last and (ps.date.today() - ps.date.fromisoformat(last)).days < NOTE_COOLDOWN_DAYS)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _meta(status: str) -> dict:
    import scan
    now = _now_iso()
    return {"generated_at": now, "status": status, "last_screening_at": now,
            "fresh_field": gate.FRESH_FIELD, "fresh_days": gate.FRESH_DAYS, "label": gate.LABEL,
            "policy_version": gate.POLICY_VERSION, "semgrep_version": scan.semgrep_version() or "",
            "ruleset_sha256": scan.ruleset_sha256()}


# ---- discovery -----------------------------------------------------------------------------------------------------
def _searches(full: bool):
    """Yield (feed_key, query, sort). full=True covers everything (weekly); otherwise a fair rotating subset."""
    run_no = datetime.now().timetuple().tm_yday * 4 + datetime.now().hour // 6
    majors = list(ps.MAJORS)
    if not full:
        majors = majors[run_no % len(majors):] + majors[:run_no % len(majors)]
    for key in majors:
        label, terms, anchor, evergreen = ps.MAJORS[key]
        for lane, (ll, n, sort, q) in ps.LANES.items():
            if not full and lane in ("oss", "research") and (lane == "oss") != (run_no % 2 == 0):
                continue
            if lane == "build":
                # cyber rotates its 8 CISSP domains AND carries the themed Python/SQL slots like the other CIS majors
                subs = (ps.cyber_rotation() + ps.BUILD_QUERIES.get("cyber", [])) if key == "cyber" \
                    else ps.BUILD_QUERIES.get(key, [(None, terms, n)])
                if key in ps.COURSES and (full or key != "cyber"):
                    subs = list(subs) + (ps.course_rotation(key) if not full else [(f"🎓 {c[0]}", c[1], 1) for c in ps.COURSES[key]])
                for sub_label, sub_terms, _n in subs:
                    yield f"{key}|build|{sub_label or ''}", q(sub_terms, anchor), sort
            else:
                yield f"{key}|{lane}|", q(terms, anchor), sort
    for sector, (sector_label, orgs) in ps.ORGS.items():
        yield f"orgs|{sector}|{sector_label}", ps.org_query(orgs), "updated"


def _static_repo_names(idx: dict):
    """(full_name, row) for every curated repo in the static sets — these are curated=True for screening."""
    st = idx.get("static", {})
    for r in (row for lst in st.get("starters", {}).values() for row in lst):
        if r and r.get("full_name"):
            yield r["full_name"], r
    for d in st.get("cyber_domains", {}).values():
        for r in d.get("repos", []):
            if r and r.get("full_name"):
                yield r["full_name"], r
    for lst in st.get("cases", {}).values():
        for item in lst:
            r = item.get("repo")
            if r and r.get("full_name"):
                yield r["full_name"], r
    for lst in st.get("projects", {}).values():
        for item in lst:
            r = item.get("repo")
            if r and r.get("full_name"):
                yield r["full_name"], r
    for stages in st.get("paths", {}).values():
        for stage in stages:
            for item in stage:
                r = item.get("repo")
                if r and r.get("full_name"):
                    yield r["full_name"], r


def _screen_all(idx: dict) -> dict:
    """Screen every repo (fresh feed rows curated=False, static rows curated=True). Reuse current records; re-screen
    new/expired ones (that is the only place a clone happens). Returns the new screening store."""
    quarantine = gate.load_quarantine()
    prior = gate.load_screening()  # last run's records (repo root)
    repos: dict[str, tuple[dict, bool]] = {}
    for rows in idx["keys"].values():
        for r in rows:
            repos.setdefault(r["full_name"], (r, False))
    for full_name, r in _static_repo_names(idx):
        repos.setdefault(full_name, (r, True))
    counts = {}
    for full_name in repos:
        k = full_name.split("/")[-1].lower()
        counts[k] = counts.get(k, 0) + 1
    store = {"records": {}}
    rescreened = reused = 0
    for full_name, (r, curated) in sorted(repos.items()):
        p = prior["records"].get(full_name)
        head = None
        if gate.record_is_current(p, None):          # cheap pre-check (policy + expiry) before spending a network call
            head = gate.head_sha(full_name)          # HIGH-2: confirm the current default-branch SHA before reuse
            if head and gate.record_is_current(p, head):
                store["records"][full_name] = p
                reused += 1
                continue
            # head is None (unreachable) or the commit changed -> must re-screen; never reuse eligibility on an
            # unconfirmed SHA. A prior record is NOT carried forward here.
        rec = gate.screen_one(r, quarantine, name_counts=counts, curated=curated, sha=head)
        if rec is None:                              # transiently unreachable this run: retry next run (no record)
            continue
        store["records"][full_name] = rec
        rescreened += 1
        if rescreened % 10 == 0:
            print(f"  screened {rescreened} (reused {reused})", file=sys.stderr)
    print(f"screening: {rescreened} re-screened, {reused} reused, {len(store['records'])} records", file=sys.stderr)
    return store


def _eligible_rows(rows: list[dict], store: dict, quarantine: dict, meta: dict, kind: str) -> list[dict]:
    out = []
    for r in rows:
        r2 = dict(r)
        r2["kind"] = kind
        rec = store["records"].get(r["full_name"])
        if rec:
            r2["screened"] = gate.screened_block(rec)
            if rec.get("sha"):  # HIGH-2: link students to the exact reviewed commit, not the mutable default branch
                r2["commit_url"] = f"https://github.com/{r['full_name']}/tree/{rec['sha']}"
        if kind == "fresh":
            lv = levels.classify(r2, rec)   # multi-signal difficulty, not stars/size alone
            r2["level"] = lv["level"]
            r2["level_meta"] = {k: lv[k] for k in ("confidence", "prereqs", "task", "effort", "success")}
        ok, _why = gate.eligible(r2, store, quarantine, meta)
        if ok:
            out.append(r2)
    return out


def _build_published(idx: dict, store: dict, meta: dict, quarantine: dict) -> dict:
    feed = {}
    for key, rows in idx["keys"].items():
        kept = _eligible_rows(rows, store, quarantine, meta, "fresh")
        if not key.startswith("orgs|"):
            # keep only undergraduate levels; drop 'exclude' (too advanced) and 'unknown' (not reliably classifiable)
            kept = [r for r in kept if r.get("level") in levels.FEED_LEVELS]
        if kept:
            feed[key] = kept
    st = idx.get("static", {})
    ever = {"stages": st.get("stages", []), "starters": {}, "cyber_domains": {}, "cases": {}, "paths": {}, "projects": {}}

    def keep_repo(item_repo):
        if not item_repo or not item_repo.get("full_name"):
            return None
        k = _eligible_rows([item_repo], store, quarantine, meta, "evergreen")
        return k[0] if k else None

    for major, lst in st.get("starters", {}).items():
        rows = [x for x in (keep_repo(r) for r in lst) if x]
        if rows:
            ever["starters"][major] = rows
    for d, dv in st.get("cyber_domains", {}).items():
        rows = [x for x in (keep_repo(r) for r in dv.get("repos", [])) if x]
        ever["cyber_domains"][d] = {"label": dv.get("label"), "ref": dv.get("ref"), "repos": rows}
    for major, lst in st.get("cases", {}).items():
        items = []
        for item in lst:
            repo = keep_repo(item.get("repo")) if item.get("repo") else None
            if item.get("repo") and not repo:
                continue  # a GitHub case repo that failed screening is dropped; non-repo (URL) cases stay
            items.append({"target": item.get("target"), "blurb": item.get("blurb"), "repo": repo})
        if items:
            ever["cases"][major] = items
    for major, stages in st.get("paths", {}).items():
        new_stages = []
        for stage in stages:
            new_stage = []
            for item in stage:
                repo = keep_repo(item.get("repo")) if item.get("repo") else None
                if item.get("repo") and not repo:
                    repo = None  # keep the step, drop the dead/failed repo link (the todo text still guides the student)
                new_stage.append({"target": item.get("target"), "todo": item.get("todo"), "repo": repo})
            new_stages.append(new_stage)
        ever["paths"][major] = new_stages
    for major, lst in st.get("projects", {}).items():
        items = []
        for item in lst:
            repo = keep_repo(item.get("repo")) if item.get("repo") else None
            if item.get("repo") and not repo:
                continue          # a curated project repo that failed screening is dropped entirely, task and all
            items.append({"level": item.get("level"), "lang": item.get("lang"), "name": item.get("name"),
                          "todo": item.get("todo"), "repo": repo})
        if items:
            ever["projects"][major] = items
    hacks = idx.get("hackathons", {})
    items = [h for h in hacks.get("items", []) if _hack_ok(h)]
    hugging = idx.get("hf", {})
    hf_items = {m: [r for r in (rows or []) if hf.eligible(r)] for m, rows in (hugging.get("items") or {}).items()}
    return {"meta": meta, "feed": feed, "evergreen": ever,
            "hackathons": {"at": hacks.get("at", meta["generated_at"]), "items": items},
            "huggingface": {"at": hugging.get("at", meta["generated_at"]),
                            "items": {m: rows for m, rows in hf_items.items() if rows}},
            "fallback": SYNTHETIC}


def _hack_ok(h: dict) -> bool:
    c = links.classify(h.get("url", ""))
    return not (c and c[0] == "block")


def _all_repo_rows(pub: dict):
    """Yield (section, row) for EVERY repository-bearing row in a published snapshot — fresh feed, orgs, starters,
    cyber-domain repos, case repos and learning-path repos. The independent validator checks every one."""
    for key, rows in (pub.get("feed") or {}).items():
        for r in rows:
            yield f"feed:{key}", r
    ev = pub.get("evergreen") or {}
    for major, rows in (ev.get("starters") or {}).items():
        for r in rows:
            yield f"starters:{major}", r
    for d, dv in (ev.get("cyber_domains") or {}).items():
        for r in (dv.get("repos") or []):
            yield f"cyber_domains:{d}", r
    for major, items in (ev.get("cases") or {}).items():
        for it in items:
            if it.get("repo"):
                yield f"cases:{major}", it["repo"]
    for major, items in (ev.get("projects") or {}).items():
        for it in items:
            if it.get("repo"):
                yield f"projects:{major}", it["repo"]
    for major, stages in (ev.get("paths") or {}).items():
        for stage in stages:
            for it in stage:
                if it.get("repo"):
                    yield f"paths:{major}", it["repo"]


def validate_published(pub: dict, store: dict, quarantine: dict, meta: dict) -> tuple[bool, list[str]]:
    """Independently re-validate every repository-bearing row against the AUTHORITATIVE screening records and the
    quarantine — malformed records, missing engines, incomplete coverage, expired scans, missing/mismatched SHAs and
    quarantined repos are all rejected. Returns (ok, problems). Does not trust screened fields in the rows beyond the
    SHA cross-check that eligible() performs against the record."""
    problems = []
    for section, row in _all_repo_rows(pub):
        if not isinstance(row, dict) or not isinstance(row.get("full_name"), str):
            problems.append(f"{section}: malformed row")
            continue
        ok, why = gate.eligible(row, store, quarantine, meta)
        if not ok:
            problems.append(f"{section} {row.get('full_name')}: {why[0] if why else 'ineligible'}")
        if len(problems) >= 50:
            break
    # Hugging Face rows are not repositories and never claim a code scan; they are re-validated against their own
    # record the same way (passing screen, pinned SHA, not expired, link-screened URL).
    for major, rows in ((pub.get("huggingface") or {}).get("items") or {}).items():
        for r in rows:
            if not isinstance(r, dict) or not str(r.get("full_name", "")).startswith("hf:"):
                problems.append(f"huggingface:{major}: malformed row")
            elif not hf.eligible(r):
                problems.append(f"huggingface:{major} {r.get('full_name')}: screening missing, expired or not passing")
    return (not problems), problems


def _meta_from_store(store: dict) -> dict:
    """Build snapshot meta from the authoritative screening store (not from any artifact-supplied meta)."""
    m = _meta("operational")
    passing = sum(1 for r in store.get("records", {}).values() if r.get("result") == "pass")
    m["last_screening_at"] = store.get("generated_at") or m["generated_at"]
    m["status"] = "operational" if passing >= MIN_PASSING else "degraded"
    return m


def _drop_sections(published: dict, seen: dict) -> list[dict]:
    """New, eligible, fresh repos not yet sent — grouped for the push. BEGINNER is the default here; a major with no
    new beginner repo gets a self-contained synthetic BEGINNER project instead of a harder repo. Intermediate and
    challenge repos are offered on demand via /scout level:, not pushed."""
    out = []
    order = list(ps.MAJORS)
    for major in order:
        label = ps.MAJORS[major][0]
        parts = []
        for lane, tag in (("build", "🧪 Build this"), ("oss", "🤝 Contribute — open good-first-issues"),
                          ("research", "🔬 Research — fresh paper code")):
            rows = []
            for key, krows in published["feed"].items():
                if key.startswith(f"{major}|{lane}|"):
                    rows += [r for r in krows if r["full_name"] not in seen and r.get("level") == "beginner"]
            rows = ps.ordered(rows, 0)[:2]
            if rows:
                for r in rows:
                    seen[r["full_name"]] = ps.date.today().isoformat()
                parts.append({"lane": lane, "header": tag, "rows": rows})
        note = None if parts else SYNTHETIC[major]  # no beginner repo cleared this slot -> synthetic beginner, never harder
        # Hugging Face rows ride in the same section (same channel, same seen-dedupe) but are their own lane: they are
        # link-screened, not code-scanned, and they never stand in for a beginner project (hence: after `note`).
        hf_rows = [r for r in (published.get("huggingface", {}).get("items", {}).get(major) or [])
                   if r["full_name"] not in seen][:hf.PICKS]
        if hf_rows:
            for r in hf_rows:
                seen[r["full_name"]] = ps.date.today().isoformat()
            parts.append({"lane": "hf", "header": hf.HEADER, "rows": hf_rows})
        if parts or note:
            out.append({"key": major, "label": label, "sections": parts, "note": note})
    # orgs
    org_rows = []
    for key, krows in published["feed"].items():
        if key.startswith("orgs|"):
            org_rows += [r for r in krows if r["full_name"] not in seen]
    org_rows = ps.ordered(org_rows)[:4]
    if org_rows:
        for r in org_rows:
            seen[r["full_name"]] = ps.date.today().isoformat()
        out.append({"key": "orgs", "label": "🤝 Mission-driven orgs", "orgs": org_rows})
    # hackathons
    new_hacks = [h for h in published["hackathons"]["items"] if "hack:" + h["url"] not in seen]
    for h in new_hacks:
        seen["hack:" + h["url"]] = ps.date.today().isoformat()
    if new_hacks:
        out.append({"key": "hackathons", "label": "🏁 NYC in-person hackathons", "hacks": new_hacks[:8]})
    return out


def discover(out_dir: str, full: bool = False) -> int:
    os.makedirs(out_dir, exist_ok=True)
    ps.load_index()
    for key, q, sort in _searches(full):
        try:
            found = ps.gh_search(q, sort)
            ps.index_add(key, found)
        except ps.BudgetExceeded as e:
            print(f"discover stopped early: {e}", file=sys.stderr)
            break
        except Exception as e:
            print(f"{key}: {e}", file=sys.stderr)
    try:
        ps.refresh_static()
    except Exception as e:
        print(f"static refresh: {e}", file=sys.stderr)
    ps._index["hackathons"] = {"at": _now_iso(), "items": ps.hackathons_nyc()}
    try:
        ps._index["hf"] = {"at": _now_iso(), "items": hf.discover()}   # screened here; rows expire in 14 days
    except Exception as e:
        print(f"hugging face: {e}", file=sys.stderr)                   # keep the previous rows; they expire on their own
    idx = ps._index
    store = _screen_all(idx)
    passing = sum(1 for r in store["records"].values() if r.get("result") == "pass")
    meta = _meta("operational" if passing >= MIN_PASSING else "degraded")
    quarantine = gate.load_quarantine()
    published = _build_published(idx, store, meta, quarantine)
    seen = ps.load_seen()
    drop = _drop_sections(published, dict(seen))  # dict(seen): compute drop without mutating the on-disk seen yet
    json.dump(idx, open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8"), separators=(",", ":"), ensure_ascii=False)
    gate.save_screening(store, os.path.join(out_dir, "screening.json"))
    json.dump(published, open(os.path.join(out_dir, "published.json"), "w", encoding="utf-8"), separators=(",", ":"), ensure_ascii=False)
    json.dump({"drop": drop, "meta": meta}, open(os.path.join(out_dir, "drop.json"), "w", encoding="utf-8"), ensure_ascii=False)
    print(f"discover: {len(store['records'])} screened ({passing} passing), "
          f"{sum(len(v) for v in published['feed'].values())} feed rows, {len(drop)} drop sections, status={meta['status']}")
    return 0


# ---- publish -------------------------------------------------------------------------------------------------------
_ORG_SECTOR = {o.lower(): sector_label for sector_label, orgs in ps.ORGS.values() for o in orgs}


def _render_section(sec: dict) -> str:
    if "orgs" in sec:
        return "\n<b>🤝 Contribute to mission-driven orgs — resume-ready experience</b>\n" + \
            "\n".join(ps.render_org(r, _ORG_SECTOR.get(r["full_name"].split("/")[0].lower(), "🏢")) for r in sec["orgs"])
    if "hacks" in sec:
        return "\n<b>🏁 NYC in-person hackathons — new listings</b>\n<i>Spring requirement: attend one.</i>\n" + \
            "\n".join(ps.render_hack(h) for h in sec["hacks"]) + ps.hack_footer()
    parts = [f"\n<b>{sec['label']}</b> <i>· 🟢 Beginner by default — /scout level:intermediate or level:challenge for more</i>"]
    for part in sec.get("sections", []):
        if part["lane"] == "hf":
            parts.append(f"<i>{part['header']} · {hf.NOTE}</i>\n" + "\n".join(hf.render(r) for r in part["rows"]))
            continue
        parts.append(f"<i>{part['header']}</i>\n" + "\n".join(ps.render_repo(r, part["lane"]) for r in part["rows"]))
    if sec.get("note"):
        parts.append("<i>🟢 Beginner build-it-yourself idea (no beginner repo cleared screening this slot)</i>\n• "
                     + ps.esc(sec["note"]))
    return "\n".join(parts)


LABEL_LINE = f"\n<i>ℹ️ {gate.LABEL}</i>"


def publish(in_dir: str, index_only: bool = False) -> int:
    idx = json.load(open(os.path.join(in_dir, "index.json"), encoding="utf-8"))
    store = gate.load_screening(os.path.join(in_dir, "screening.json"))  # authoritative screening records
    drop = json.load(open(os.path.join(in_dir, "drop.json"), encoding="utf-8")).get("drop", [])
    quarantine = gate.load_quarantine()

    # HIGH-4: RECONSTRUCT the snapshot from index + authoritative screening records (never trust the artifact's own
    # published.json rows), then INDEPENDENTLY VALIDATE every repository-bearing section before writing anything.
    meta = _meta_from_store(store)
    published = _build_published(idx, store, meta, quarantine)
    ok, problems = validate_published(published, store, quarantine, meta)
    if not ok:
        for p in problems[:10]:
            print(f"  reject: {p}", file=sys.stderr)
        print(f"publish: snapshot VALIDATION FAILED ({len(problems)} problem(s)); previous published.json preserved, "
              f"nothing sent", file=sys.stderr)
        return 1  # non-zero → the workflow's failure alert fires; root published.json is left untouched

    # validation passed: write the reconstructed snapshot the Workers read (and the debugging index/screening)
    json.dump(published, open("published.json", "w", encoding="utf-8"), separators=(",", ":"), ensure_ascii=False)
    json.dump(idx, open("index.json", "w", encoding="utf-8"), separators=(",", ":"), ensure_ascii=False)
    gate.save_screening(store, "screening.json")

    if index_only:
        print(f"publish-index: validated + wrote published.json ({sum(len(v) for v in published['feed'].values())} rows), status={meta['status']}")
        return 0

    seen = ps.load_seen()
    sent = 0
    telegram_chunks, discord_posts = [], []
    for sec in drop:
        # OFFLINE re-validation: every row must still pass the gate right now, and not already be sent
        if "sections" in sec:
            for part in sec["sections"]:
                ok = (lambda r: hf.eligible(r)) if part["lane"] == "hf" else \
                     (lambda r: gate.eligible(r, store, quarantine, meta)[0])
                part["rows"] = [r for r in part["rows"] if r["full_name"] not in seen and ok(r)]
            sec["sections"] = [p for p in sec["sections"] if p["rows"]]
            if sec.get("note") and _note_recent(seen, sec["key"]):
                sec.pop("note")  # same synthetic idea as last time; posting it again is just noise
            if not sec["sections"] and not sec.get("note"):  # keep a section that carries a synthetic beginner idea
                continue
        elif "orgs" in sec:
            sec["orgs"] = [r for r in sec["orgs"] if r["full_name"] not in seen and gate.eligible(r, store, quarantine, meta)[0]]
            if not sec["orgs"]:
                continue
        elif "hacks" in sec:
            sec["hacks"] = [h for h in sec["hacks"] if "hack:" + h["url"] not in seen and _hack_ok(h)]
            if not sec["hacks"]:
                continue
        text = _render_section(sec)
        if sec.get("key") not in ("orgs", "hackathons"):
            text += LABEL_LINE
        telegram_chunks.append(text)
        discord_posts.append((sec["key"], text))
        for part in sec.get("sections", []):
            for r in part["rows"]:
                seen[r["full_name"]] = ps.date.today().isoformat()
        for r in sec.get("orgs", []):
            seen[r["full_name"]] = ps.date.today().isoformat()
        for h in sec.get("hacks", []):
            seen["hack:" + h["url"]] = ps.date.today().isoformat()
        if sec.get("note"):
            seen["note:" + sec["key"]] = ps.date.today().isoformat()
        sent += 1

    # merged case-library PRs (read-only public data), still gated for links by the Worker allowlist on render
    for pr in ps.merged_cases(seen):
        t = ("\n<b>📁 New case merged in the TLDP library</b>\n"
             f'• <a href="{pr["html_url"]}">{ps.esc(pr["title"])}</a> by {ps.esc(pr["user"]["login"])}')
        telegram_chunks.append(t)
        discord_posts.append(("cases", t))
        sent += 1

    if not sent:
        # Nothing qualified this run: offer ONE rotating self-contained idea (no external links) instead of silence.
        run_no = datetime.now().timetuple().tm_yday * 4 + datetime.now().hour // 6
        major = list(SYNTHETIC)[run_no % len(SYNTHETIC)]
        idea = SYNTHETIC[major]
        text = (f"\n<b>{ps.MAJORS[major][0]} — build-it-yourself idea</b>\n<i>No fresh repo cleared screening for this "
                f"slot, so here is a self-contained project you can start now with no downloads.</i>\n• {ps.esc(idea)}" + LABEL_LINE)
        telegram_chunks.append(text)
        discord_posts.append((major, text))

    if "--preview" in sys.argv:
        print("\n\n=====\n\n".join(ps.messages([ps.HEADER] + telegram_chunks)))
        return 0
    for m in ps.messages([ps.HEADER] + telegram_chunks + [ps.FOOTER]):
        ps.send(m)
    for key, text in discord_posts:
        ps.discord(key, text)
    json.dump(seen, open(ps.SEEN, "w"), indent=0)
    print(f"publish: {sent} section(s) sent, status={meta['status']}, {sum(len(v) for v in published['feed'].values())} eligible feed rows")
    return 0
