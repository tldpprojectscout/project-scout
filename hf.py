#!/usr/bin/env python3
"""
hf.py — the Hugging Face lane: models, datasets and Spaces as student project material.

Shaped like the hackathons feed, NOT like the GitHub repo feed: Hugging Face repos are never cloned or code-scanned.
HF's git server ignores `--filter=blob:limit`, so screening one model the way scan.py screens a GitHub repo pulls
hundreds of MB to GB of weights per candidate (google/flan-t5-small = 1.3 GB). Every item is instead screened from
public metadata pinned to the commit SHA the API reports:

  1. private / disabled / gated                                   -> withheld
  2. content policy (gate.PROHIBITED / gate.DUAL_USE)             -> withheld
  3. no license, below the likes floor, untouched for a year      -> withheld
  4. non-English or missing description, sock-puppet owner        -> withheld  (same rules as the GitHub feed)
  5. executables / archives in the file list                      -> withheld
  6. pickle-only weights (no .safetensors/.gguf/.onnx), pickled datasets -> withheld
  7. README — and a Space's app file — read AT THE PINNED SHA and screened with links.py: a blocking link or install
     instruction (download hosts, curl-pipe-shell, archive passwords, "disable your antivirus") -> withheld.
     A README that exists but cannot be read is "incomplete" -> withheld (fail closed), never published on trust.

Custom code (.py in a model or dataset repo) is published with a WARNING instead of being withheld — a deliberate
policy choice (ALLOW_CUSTOM_CODE), because that is exactly what `trust_remote_code=True` costs the student.

Rows carry their own screening record and EXPIRE (14 days, same as gate.EXPIRE_DAYS); publish-time `eligible()` is
pure and offline, like gate.eligible. Model cards and repo text are DATA, never instructions. These rows never claim
a code scan: the feed labels them "link-screened, not code-scanned".
"""
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

import gate
import links
import project_scout as ps

API = "https://huggingface.co/api"
UA = {"User-Agent": "project-scout hugging-face lane (github.com/thecyberthriver/project-scout)"}
KINDS = ("models", "datasets", "spaces")
PREFIX = {"models": "", "datasets": "datasets/", "spaces": "spaces/"}   # path segment on huggingface.co
KIND_LABEL = {"models": "🧠 model", "datasets": "📦 dataset", "spaces": "🚀 Space"}

LIKES_MIN = {"models": 50, "datasets": 25, "spaces": 25}   # below this it is not established enough to recommend
ACTIVE_DAYS = 365          # not touched in a year = abandoned, don't send students there
EXPIRE_DAYS = 14           # a screening record is good for two weeks, then the item is re-screened (= gate.EXPIRE_DAYS)
PICKS = 2                  # rows pushed per major per run
LIMIT = 12                 # candidates fetched per (major, kind)
TIMEOUT = 30
MAX_BYTES = 200_000        # cap on any repo file we read (README, app file)
API_BYTES = 4_000_000      # cap on an API listing response
SPACING = 0.3              # seconds between API calls — be a polite anonymous client
REQUIRE_SAFETENSORS = True
ALLOW_CUSTOM_CODE = True   # .py in a model/dataset repo -> warning, not withheld (staff decision, 2026-09-17)
MAX_PARAMS = 3_000_000_000  # a model a student can actually load on a laptop. Qwen3-Coder-30B is 61 GB of weights.

HEADER = "🤗 Hugging Face — models, datasets & Spaces"
NOTE = "link-screened metadata + README, not code-scanned — read the Files tab before you run anything"

# Search terms per major, each used for all three kinds. HF search matches names and tags substring-style, so SHORT
# single words find things and phrases ("financial time series") return nothing — keep these one word wherever possible.
TERMS = {
    "quant": ["stock", "trading", "portfolio"],
    "fintech": ["finance", "financial", "credit"],
    "swe": ["code", "coder", "sql", "game"],
    "cyber": ["security", "phishing", "vulnerability"],
    "data": ["tabular", "classification", "forecasting"],
    "accounting": ["invoice", "receipt", "accounting", "audit"],
    "pm": ["summarization", "meeting"],
    "marketing": ["sentiment", "marketing", "reviews", "twitter"],
}

# Industry axis. Free: it is read off text we already have, so it costs no extra API call. First match wins; an item
# that matches nothing is "general" and is simply not labelled.
INDUSTRY = [
    ("healthcare", "🩺", r"health|medical|clinical|patient|hospital|biomed|radiolog|\behr\b|hipaa|diagnos|drug|disease|cancer"),
    ("finance", "🏦", r"financ|bank|credit|loan|invest|stock|trading|portfolio|insur|fraud|payment|accounting|earnings"),
    ("retail", "🛒", r"retail|e-?commerce|shopping|product review|inventory|supply chain|customer review|amazon review|sales forecast"),
    ("hospitality", "🏨", r"hotel|restaurant|travel|tourism|booking|airline|flight|hospitality|menu|recipe|yelp"),
    ("gaming", "🎮", r"\bgam(e|es|ing)\b|video ?game|esports|minecraft|roblox|twitch|nintendo|steam review|npc dialogue"),
    ("social media", "📱", r"social media|twitter|\btweets?\b|reddit|instagram|tiktok|facebook|hashtag|influencer|toxic comment|youtube comment|\bmemes?\b"),
    ("education", "🎓", r"education|student|course|classroom|school|exam|tutor|essay scoring"),
    ("public sector", "🏛", r"government|public sector|civic|municipal|census|regulation|legal|court|policy document"),
]
INDUSTRY_RX = [(name, emoji, re.compile(rx, re.I)) for name, emoji, rx in INDUSTRY]


def industry_of(text: str) -> tuple[str, str]:
    """(name, emoji) for the first industry this item's own text matches; ("", "") when it is general-purpose."""
    for name, emoji, rx in INDUSTRY_RX:
        if rx.search(text or ""):
            return name, emoji
    return "", ""


# What a student actually DOES with an item, per kind and task. An item we cannot hand a first move for is not
# published at all: a link with no first step is how a project dies before it starts. Each recipe is
# (what to install, three steps, what "done" looks like). {id} is substituted with the repo id.
DATASET_STEPS = ("pip install datasets pandas matplotlib", [
    "Open the Dataset Viewer on the page and read 20 rows. Before any code, write down ONE question you want it to answer.",
    'Load it: `from datasets import load_dataset` · `d = load_dataset("{id}", split="train").to_pandas()` · `d.head()`.',
    "Answer your question with one groupby and one chart, then post the chart in #show-your-work.",
], "you can explain one thing the data shows that you did not know before")

SPACE_STEPS = ("a free Hugging Face account", [
    "Open the Space and try three inputs of your own — note where it does badly, that gap is your project.",
    "Click ⋮ → Duplicate this Space. You now own a running copy; nothing to install.",
    "Change ONE thing in app.py (a prompt, a threshold, the model) and redeploy. Compare before and after.",
], "your copy behaves differently from the original and you can say why")

# model task -> the same shape. Tasks absent from this table are not published.
TASK_STEPS = {
    "text-classification": ("pip install transformers torch", [
        'Run it: `from transformers import pipeline` · `p = pipeline("text-classification", model="{id}")` · `p("your sentence")`.',
        "Write 20 sentences of your own and label them yourself first, then compare the model's answers to your labels.",
        "Report its accuracy on your 20 and show the ones it got wrong — the mistakes are the interesting part.",
    ], "you can name one kind of sentence this model reliably gets wrong"),
    "token-classification": ("pip install transformers torch", [
        'Run it: `p = pipeline("token-classification", model="{id}")` on a paragraph of news text.',
        "Feed it 10 paragraphs from a domain you care about and count what it misses.",
        "Build a tiny script that extracts every entity from a folder of text files into a CSV.",
    ], "your CSV has the entities from 10 documents and you know its error rate"),
    "question-answering": ("pip install transformers torch", [
        'Run it: `p = pipeline("question-answering", model="{id}")` with a context paragraph and a question.',
        "Paste in a page of your own course notes and ask it five questions.",
        "Wrap it in a loop that answers a list of questions about one document and prints the confidence.",
    ], "it answers five questions about your own notes and you can judge each answer"),
    "summarization": ("pip install transformers torch", [
        'Run it: `p = pipeline("summarization", model="{id}")` on one long article.',
        "Summarise five articles you have actually read, and mark each summary good or bad yourself.",
        "Add a length setting and compare a short summary to a long one for the same article.",
    ], "you can say when this model's summaries are trustworthy and when they are not"),
    "translation": ("pip install transformers torch sentencepiece", [
        'Run it: `p = pipeline("translation", model="{id}")` on five sentences.',
        "Translate text you can check yourself, or have a classmate who speaks the language check it.",
        "Build a script that translates a file line by line and flags lines it left unchanged.",
    ], "you have five checked translations and a list of what it failed on"),
    "zero-shot-classification": ("pip install transformers torch", [
        'Run it: `p = pipeline("zero-shot-classification", model="{id}")` with your own candidate labels.',
        "Sort 30 real items (emails, tickets, posts) into your labels and check the results by hand.",
        "Tune the label wording — small changes move the results a lot. Record what worked.",
    ], "you can show how label wording changed the accuracy on your 30 items"),
    "fill-mask": ("pip install transformers torch", [
        'Run it: `p = pipeline("fill-mask", model="{id}")` with a sentence containing the mask token.',
        "Try 10 sentences from your field and see whether the top prediction is domain-aware.",
        "Use it to build a small quiz: hide a word, ask a classmate, compare to the model.",
    ], "you can say whether this model knows your subject area"),
    "sentence-similarity": ("pip install sentence-transformers", [
        'Run it: `from sentence_transformers import SentenceTransformer` · `m = SentenceTransformer("{id}")`.',
        "Embed 50 short texts of your own and find the closest pair with cosine similarity.",
        "Build a tiny search box: type a query, return the three closest items.",
    ], "your search returns sensible matches over your own 50 items"),
    "feature-extraction": ("pip install sentence-transformers", [
        'Run it: `from sentence_transformers import SentenceTransformer` · `m = SentenceTransformer("{id}")`.',
        "Embed 50 short texts of your own and find the closest pair with cosine similarity.",
        "Build a tiny search box: type a query, return the three closest items.",
    ], "your search returns sensible matches over your own 50 items"),
    "automatic-speech-recognition": ("pip install transformers torch librosa", [
        'Run it: `p = pipeline("automatic-speech-recognition", model="{id}")` on a 30-second clip you record.',
        "Transcribe five clips with different accents or background noise and count the word errors.",
        "Make it write an .srt subtitle file with timestamps.",
    ], "you have subtitles for your own clip and a word-error count"),
    "image-classification": ("pip install transformers torch pillow", [
        'Run it: `p = pipeline("image-classification", model="{id}")` on one of your own photos.',
        "Try 20 photos, including deliberately hard ones, and record what it confuses.",
        "Sort a folder of images into subfolders by the predicted label.",
    ], "your folder is sorted and you can describe its failure pattern"),
    "text-to-image": ("pip install diffusers torch", [
        "Generate one image from the example prompt on the model page — start with the page's own settings.",
        "Change one thing at a time (prompt wording, steps, seed) and keep a table of what each change did.",
        "Produce a set of five images that belong together — a small asset pack, consistent in style.",
    ], "you have five consistent images and notes on which setting did what"),
    "text-generation": ("pip install transformers torch", [
        'Run it: `p = pipeline("text-generation", model="{id}")` with a prompt from your own coursework.',
        "Run the same prompt at temperature 0.2 and 1.0 five times each. Write down the difference you see.",
        "Wrap it in a command-line tool that takes a prompt and prints the answer.",
    ], "your CLI answers a prompt and you can explain what temperature changed"),
}
TASK_ALIAS = {"text2text-generation": "summarization", "image-to-text": "image-classification",
              "audio-classification": "image-classification", "sentence-transformers": "sentence-similarity"}


def recipe(kind: str, task: str) -> tuple[str, list[str], str] | None:
    """(prereq, steps, done_when) for this item, or None when we have no honest first move to offer."""
    if kind == "datasets":
        return DATASET_STEPS
    if kind == "spaces":
        return SPACE_STEPS
    t = TASK_ALIAS.get(task or "", task or "")
    return TASK_STEPS.get(t)


def language_ok(item: dict) -> bool:
    """English data only. HF tags carry `language:xx`; no language tag at all (most code/vision repos) is fine."""
    langs = {t.split(":", 1)[1].lower() for t in (item.get("tags") or [])
             if isinstance(t, str) and t.startswith("language:")}
    return not langs or bool(langs & {"en", "eng", "english", "multilingual"})


def params_of(item_id: str, fetch=None) -> int | None:
    """Parameter count from the model's own API record. None = unknown (GGUF-only repos carry no count)."""
    fetch = fetch or _get
    try:
        d = json.loads(fetch(f"{API}/models/{urllib.parse.quote(item_id)}", cap=API_BYTES) or b"{}")
    except Exception:                                            # noqa: BLE001 — unknown size fails closed below
        return None
    total = (d.get("safetensors") or {}).get("total")
    return int(total) if isinstance(total, (int, float)) and total > 0 else None


EXEC_FILE = re.compile(r"\.(exe|msi|dll|apk|scr|bat|cmd|vbs|vbe|jse|hta|jar|iso|img|dmg|pkg|lnk|zip|rar|7z)$", re.I)
CODE_FILE = re.compile(r"\.(py|sh|ipynb)$", re.I)
PICKLE_FILE = re.compile(r"\.(bin|pt|pth|ckpt|pkl|pickle|joblib|h5|msgpack)$", re.I)
SAFE_WEIGHTS = re.compile(r"\.(safetensors|gguf|onnx)$", re.I)
PICKLED_DATA = re.compile(r"\.(pkl|pickle|joblib|pt|pth)$", re.I)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _get(url: str, accept_404: bool = False, cap: int = MAX_BYTES) -> bytes | None:
    """GET at most `cap` bytes. None = a 404 the caller said is acceptable. Raises on anything else (fail closed)."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=TIMEOUT) as r:
            return r.read(cap)
    except urllib.error.HTTPError as e:
        if accept_404 and e.code == 404:
            return None
        raise


def url_for(kind: str, item_id: str) -> str:
    return "https://huggingface.co/" + PREFIX[kind] + urllib.parse.quote(item_id)


def raw_url(kind: str, item_id: str, sha: str, path: str) -> str:
    return f"https://huggingface.co/{PREFIX[kind]}{urllib.parse.quote(item_id)}/raw/{sha}/{urllib.parse.quote(path)}"


def search(kind: str, terms: str, limit: int = LIMIT) -> list[dict]:
    q = urllib.parse.urlencode({"search": terms, "sort": "likes", "direction": -1, "limit": limit, "full": "true"})
    time.sleep(SPACING)
    data = json.loads(_get(f"{API}/{kind}?{q}", cap=API_BYTES) or b"[]")
    return data if isinstance(data, list) else []


def license_of(item: dict) -> str:
    lic = (item.get("cardData") or {}).get("license")
    if isinstance(lic, list):
        lic = lic[0] if lic else None
    if not lic:
        for t in item.get("tags") or []:
            if isinstance(t, str) and t.startswith("license:"):
                lic = t.split(":", 1)[1]
                break
    return str(lic or "")


def describe(item: dict, kind: str) -> str:
    """A model has no description field — build one from what the card does carry."""
    d = (item.get("description") or "").strip()
    if d:
        return re.sub(r"\s+", " ", d)[:200]
    card = item.get("cardData") or {}
    bits = [card.get("title"), item.get("pipeline_tag"), item.get("library_name") or item.get("sdk")]
    bits += [t for t in (item.get("tags") or []) if isinstance(t, str) and ":" not in t]
    out: list[str] = []
    for b in bits:                                    # the same word arrives as library_name AND as a tag
        if b and str(b).lower() not in [x.lower() for x in out]:
            out.append(str(b))
    return re.sub(r"\s+", " ", " · ".join(out[:6]))[:200]


def files_of(item: dict) -> list[str]:
    return [s.get("rfilename", "") for s in (item.get("siblings") or []) if isinstance(s, dict)]


def app_file(item: dict) -> str:
    """The file a Space actually runs. cardData.app_file when declared, else the conventional entry point."""
    named = (item.get("cardData") or {}).get("app_file")
    if isinstance(named, str) and named.strip():
        return named.strip()
    names = files_of(item)
    for candidate in ("app.py", "streamlit_app.py", "main.py", "index.html", "Dockerfile"):
        if candidate in names:
            return candidate
    return ""


def screen(item: dict, kind: str, fetch=None) -> dict:
    """Screen one HF item from metadata + its README (+ a Space's app file) at the pinned SHA.
    Returns a record: result pass | fail | incomplete, with reasons and warnings. Never raises.
    `fetch` overrides the HTTP reader (tests pass a stub; nothing else should)."""
    fetch = fetch or _get
    now = _now()
    item_id = str(item.get("id") or "")
    owner = item_id.split("/")[0] if "/" in item_id else ""
    sha = str(item.get("sha") or "")
    rec = {"at": _iso(now), "sha": sha, "kind": kind, "result": "fail", "reasons": [], "warnings": [],
           "checks": ["metadata", "content-policy", "license", "files", "readme-links"],
           "expires": _iso(now + timedelta(days=EXPIRE_DAYS))}
    reasons, warn = rec["reasons"], rec["warnings"]
    desc = describe(item, kind)
    likes = int(item.get("likes") or 0)

    if not item_id or "/" not in item_id:
        reasons.append("no owner/name id")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        reasons.append("no pinned commit SHA from the API")
    if item.get("private") or item.get("disabled"):
        reasons.append("private or disabled on the Hub")
    if item.get("gated"):
        reasons.append("gated: students cannot open it without an access request")

    # same content policy the GitHub feed uses — prohibited wording fails, dual-use stays out of the general feed
    reasons += gate.content_policy({"full_name": item_id, "description": desc})
    if ps.SCAM_WORDS.search(f"{item_id} {desc}"):
        reasons.append("scam/download-trap wording")
    if not ps.english(desc):
        reasons.append("no usable English description")
    if ps.SOCK_OWNER.match(owner) and likes < 200:
        reasons.append(f"throwaway-looking owner {owner!r} with {likes} likes")

    if not license_of(item):
        reasons.append("no license declared")
    if likes < LIKES_MIN[kind]:
        reasons.append(f"only {likes} likes (floor {LIKES_MIN[kind]})")
    touched = (item.get("lastModified") or "")[:10]
    try:
        if (now.date() - date.fromisoformat(touched)).days > ACTIVE_DAYS:
            reasons.append(f"not updated since {touched}")
    except ValueError:
        reasons.append("no last-modified date")

    # Can a student actually start this? No recipe for the task, data in a language they cannot read, or a model too
    # big to load on a laptop are all withheld — a link a student cannot act on is worse than no link.
    task = item.get("pipeline_tag") or item.get("sdk") or ""
    if not recipe(kind, task):
        reasons.append(f"no starter steps for a {kind[:-1]} of type {task or 'unknown'}")
    if not language_ok(item):
        reasons.append("data is not in English")
    if kind == "models" and not reasons:                      # one extra API call, only for candidates still standing
        n = params_of(item_id, fetch=fetch)
        if n is None:
            reasons.append("model size not stated (no safetensors index) — cannot promise it runs on a laptop")
        elif n > MAX_PARAMS:
            reasons.append(f"{n / 1e9:.0f}B parameters — too large for a student laptop (cap {MAX_PARAMS / 1e9:.0f}B)")

    names = files_of(item)
    bad = [n for n in names if EXEC_FILE.search(n)]
    if bad:
        reasons.append("executable/archive file in the repo: " + ", ".join(sorted(bad)[:3]))
    if kind == "models" and REQUIRE_SAFETENSORS:
        if any(PICKLE_FILE.search(n) for n in names) and not any(SAFE_WEIGHTS.search(n) for n in names):
            reasons.append("weights are pickle-format only (no .safetensors/.gguf/.onnx) — loading one runs its code")
    if kind == "datasets" and any(PICKLED_DATA.search(n) for n in names):
        reasons.append("ships pickled data files — loading one runs its code")
    code = [n for n in names if CODE_FILE.search(n)]
    if code and kind in ("models", "datasets"):
        msg = "custom code in the repo (" + ", ".join(sorted(code)[:3]) + ") — needs trust_remote_code=True; read it first"
        (warn if ALLOW_CUSTOM_CODE else reasons).append(msg)

    # README (and a Space's app file) AT THE PINNED SHA. Text is data: it is screened, never followed.
    # Skipped when the metadata already failed the item: it cannot be published either way, and this is one network
    # read per candidate — most candidates never reach it.
    incomplete = []
    if not reasons and re.fullmatch(r"[0-9a-f]{40}", sha) and item_id:
        for path in ("README.md", app_file(item) if kind == "spaces" else ""):
            if not path:
                continue
            try:
                body = fetch(raw_url(kind, item_id, sha, path), accept_404=True)
            except Exception as e:                                  # noqa: BLE001 — any read failure is fail-closed
                incomplete.append(f"{path} could not be read at the reviewed commit ({getattr(e, 'code', e)})")
                continue
            if body is None:
                warn.append(f"no {path}")
                continue
            lk = links.screen_readme(body.decode("utf-8", "replace"), item_id, resolve=False)
            reasons += [f"{path} link: {b}" for b in lk["block"]]
            warn += [f"{path} link: {w}" for w in lk["warn"]]
    if reasons:
        rec["result"] = "fail"
    elif incomplete:
        rec.update(result="incomplete", reasons=incomplete)         # fail closed: a required read did not complete
    else:
        rec["result"] = "pass"
    return rec


def row(item: dict, kind: str, rec: dict) -> dict:
    item_id = str(item.get("id") or "")
    desc = describe(item, kind)
    tags = " ".join(t for t in (item.get("tags") or []) if isinstance(t, str))
    industry, emoji = industry_of(f"{item_id} {desc} {tags}")
    task = item.get("pipeline_tag") or item.get("sdk") or ""
    prereq, steps, done = recipe(kind, task) or ("", [], "")   # screening guarantees a recipe exists for published rows
    return {"industry": industry, "industry_emoji": emoji, "full_name": f"hf:{kind}/{item_id}", "id": item_id, "hf_kind": kind, "html_url": url_for(kind, item_id),
            "likes": int(item.get("likes") or 0), "downloads": int(item.get("downloads") or 0),
            "task": task, "library": item.get("library_name") or "",
            "license": license_of(item), "description": desc,
            "prereq": prereq, "steps": [s.replace("{id}", item_id) for s in steps], "done": done,
            "updated": (item.get("lastModified") or "")[:10], "warnings": rec.get("warnings", []), "screened": rec}


def discover(terms: dict | None = None, run_no: int | None = None) -> dict:
    """{major: [screened rows]} — the only stage that talks to huggingface.co. Never raises for one bad item.
    An item is claimed by the first major that takes it (no duplicate across majors), so the major order ROTATES each
    run the way pipeline._searches rotates the GitHub majors — otherwise the same major always starves last."""
    out, taken = {}, set()
    src = terms or TERMS
    if run_no is None:
        now = datetime.now()
        run_no = now.timetuple().tm_yday * 4 + now.hour // 6
    order = list(src)
    order = order[run_no % len(order):] + order[:run_no % len(order)]
    for major, qs in ((m, src[m]) for m in order):
        rows = []
        for q, kind in ((q, k) for q in ([qs] if isinstance(qs, str) else qs) for k in KINDS):
            try:
                found = search(kind, q)
            except Exception as e:                                   # noqa: BLE001 — one kind failing is not fatal
                print(f"hf {major}/{kind}: {e}", file=sys.stderr)
                continue
            for item in found:
                key = f"hf:{kind}/{item.get('id')}"
                if key in taken:
                    continue
                try:
                    rec = screen(item, kind)
                except Exception as e:                               # noqa: BLE001
                    print(f"hf screen {key}: {e}", file=sys.stderr)
                    continue
                if rec["result"] != "pass":
                    continue
                taken.add(key)
                rows.append(row(item, kind, rec))
        # industry first so the section reads grouped, most-liked first inside each industry ("" = general, last)
        out[major] = sorted(rows, key=lambda r: ((r["industry"] or "zz"), -r["likes"]))[:LIMIT]
    return out


def eligible(r: dict, now: datetime | None = None) -> bool:
    """Pure, offline publish-time check — the hackathon-lane equivalent of gate.eligible. No network."""
    now = now or _now()
    rec = (r or {}).get("screened") or {}
    if rec.get("result") != "pass" or not re.fullmatch(r"[0-9a-f]{40}", str(rec.get("sha") or "")):
        return False
    try:
        if datetime.fromisoformat(rec["expires"]) <= now:
            return False
    except (KeyError, TypeError, ValueError):
        return False
    if not str(r.get("full_name", "")).startswith("hf:"):
        return False
    c = links.classify(r.get("html_url", ""))
    return not (c and c[0] == "block")


def render(r: dict) -> str:
    """One feed line, same shape as ps.render_repo. Warnings are shown to the student, not hidden."""
    kind = KIND_LABEL.get(r.get("hf_kind"), "🤗")
    ind = f'{r.get("industry_emoji", "")} {r.get("industry", "")}'.strip()
    meta = " · ".join(x for x in (ind, kind, r.get("task") or "", r.get("license") or "") if x)
    line = (f'• <a href="{r["html_url"]}">{ps.esc(r["id"])}</a> ❤{r.get("likes", 0)} · {ps.esc(meta)}\n'
            f'  {ps.esc(r.get("description") or "(no description)")}')
    for w in r.get("warnings", [])[:2]:
        line += f"\n  ⚠️ {ps.esc(w)}"
    if r.get("prereq"):
        line += f'\n  🧰 Need first: <code>{ps.esc(r["prereq"])}</code>'
    for i, s in enumerate(r.get("steps", [])[:3], 1):
        line += f"\n  {i}. {ps.esc(s)}"
    if r.get("done"):
        line += f'\n  ✅ Done when {ps.esc(r["done"])}.'
    return line


def _demo() -> None:
    """Offline self-check: the screening rules, on fixtures. No network."""
    sha = "a" * 40
    base = {"id": "acme/good-model", "sha": sha, "likes": 500, "downloads": 10, "lastModified": date.today().isoformat(),
            "cardData": {"license": "apache-2.0"}, "pipeline_tag": "text-classification",
            "siblings": [{"rfilename": "README.md"}, {"rfilename": "model.safetensors"}]}

    def rec(item, kind="models", size=b'{"safetensors": {"total": 135000000}}'):
        # offline: the API record answers the size check, every file read says "no such file"
        def fetch(url, accept_404=False, cap=None):
            return size if "/api/models/" in url else None
        return screen(dict(base, **item), kind, fetch=fetch)

    assert "no license declared" in rec({"cardData": {}, "tags": []})["reasons"]
    assert any("only 3 likes" in x for x in rec({"likes": 3})["reasons"])
    assert any("gated" in x for x in rec({"gated": True})["reasons"])
    assert any("private or disabled" in x for x in rec({"disabled": True})["reasons"])
    assert any("not updated since" in x for x in rec({"lastModified": "2019-01-01"})["reasons"])
    assert any("pickle-format only" in x for x in
               rec({"siblings": [{"rfilename": "pytorch_model.bin"}]})["reasons"])
    assert not any("pickle" in x for x in rec({"siblings": [{"rfilename": "pytorch_model.bin"},
                                                            {"rfilename": "model.safetensors"}]})["reasons"])
    assert any("executable/archive" in x for x in rec({"siblings": [{"rfilename": "setup.exe"}]})["reasons"])
    assert any("pickled data" in x for x in
               rec({"siblings": [{"rfilename": "train.pkl"}]}, "datasets")["reasons"])
    assert any("content policy" in x for x in rec({"description": "a wallet drainer for testing"})["reasons"])
    assert any("throwaway-looking owner" in x for x in rec({"id": "elenahao66/thing", "likes": 60})["reasons"])
    # custom code is a warning, not a block (ALLOW_CUSTOM_CODE)
    custom = rec({"siblings": [{"rfilename": "model.safetensors"}, {"rfilename": "modeling_acme.py"}]})
    assert not custom["reasons"] and any("trust_remote_code" in w for w in custom["warnings"]), custom

    passing = {"full_name": "hf:models/acme/good", "html_url": "https://huggingface.co/acme/good",
               "screened": {"result": "pass", "sha": sha, "expires": _iso(_now() + timedelta(days=1))}}
    assert eligible(passing)
    assert not eligible({**passing, "screened": {**passing["screened"], "expires": _iso(_now() - timedelta(days=1))}})
    assert not eligible({**passing, "screened": {**passing["screened"], "result": "incomplete"}})
    assert not eligible({**passing, "screened": {**passing["screened"], "sha": "short"}})
    assert not eligible({**passing, "full_name": "acme/good"})   # a GitHub-shaped name never rides this lane
    assert app_file({"cardData": {"app_file": "run.py"}}) == "run.py"
    assert app_file({"siblings": [{"rfilename": "app.py"}]}) == "app.py"
    assert license_of({"tags": ["license:mit"]}) == "mit"
    assert industry_of("patient triage notes")[0] == "healthcare"
    assert industry_of("hotel booking reviews")[0] == "hospitality"
    assert any("too large" in x for x in rec({}, size=b'{"safetensors": {"total": 30500000000}}')["reasons"])
    assert any("size not stated" in x for x in rec({}, size=b"{}")["reasons"])
    assert any("no starter steps" in x for x in rec({"pipeline_tag": "image-text-to-video"})["reasons"])
    assert any("not in English" in x for x in rec({"tags": ["language:ko"]})["reasons"])
    assert recipe("datasets", "") and recipe("spaces", "gradio") and recipe("models", "summarization")
    assert recipe("models", "image-to-video") is None
    assert industry_of("npc dialogue for a video game")[0] == "gaming"
    assert industry_of("toxic comment detection on reddit")[0] == "social media"
    assert industry_of("a general text model")[0] == ""
    assert row({"id": "a/b", "description": "hospital readmission"}, "datasets", {})["industry"] == "healthcare"
    assert "❤500" in render(row(dict(base), "models", {"result": "pass", "sha": sha, "warnings": ["x"]}))
    print("hf self-check ok")


if __name__ == "__main__":
    if "--preview" in sys.argv:
        sys.stdout.reconfigure(errors="replace")   # Windows consoles are cp1252; the feed itself is UTF-8 over HTTP
        for major, rows in discover().items():
            print(f"\n== {major} ==")
            for r in rows[:PICKS]:
                print(render(r))
    else:
        _demo()
