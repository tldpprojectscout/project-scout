"""End-to-end proof that unqualified repos never reach a student-visible published.json or drop section."""
import unittest
from datetime import datetime, timedelta, timezone

import gate
import pipeline

NOW = datetime.now(timezone.utc)
FRESH = (NOW - timedelta(days=2)).date().isoformat()
OLD = (NOW - timedelta(days=90)).date().isoformat()


def rec(full, result="pass", sha="a" * 40, expires_days=10):
    return {"full_name": full, "result": result, "sha": sha, "policy_version": gate.POLICY_VERSION,
            "at": NOW.isoformat(), "expires": (NOW + timedelta(days=expires_days)).isoformat(),
            "coverage": {"code_scan_result": "pass",
                         "engines": {"semgrep": "ran", "yara": "ran", "clamav": "ran", "osv": "ran"},
                         "signals": {"readme_bytes": 500, "has_tests": False, "dep_count": 2, "top_level_entries": 8}},
            "reasons": []}  # signals -> classifies as beginner, so the row survives the level filter


def frow(full, pushed=FRESH):
    return {"full_name": full, "html_url": f"https://github.com/{full}", "stargazers_count": 5, "language": "Python",
            "description": "x", "size": 300, "pushed_at": pushed, "created_at": pushed}


class Published(unittest.TestCase):
    def setUp(self):
        self.meta = pipeline._meta("operational")
        self.quar = gate.load_quarantine()

    def build(self, keys, records):
        idx = {"keys": keys, "static": {"starters": {}, "cyber_domains": {}, "cases": {}, "paths": {}, "projects": {}, "stages": []},
               "hackathons": {"at": self.meta["generated_at"], "items": []}}
        store = {"policy_version": gate.POLICY_VERSION, "records": records}
        return pipeline._build_published(idx, store, self.meta, self.quar)

    def test_only_passing_fresh_rows_reach_feed(self):
        keys = {"swe|build|": [frow("good/a"), frow("bad/b"), frow("stale/c", pushed=OLD), frow("unscreened/d")]}
        records = {"good/a": rec("good/a"), "bad/b": rec("bad/b", result="fail"), "stale/c": rec("stale/c")}
        pub = self.build(keys, records)
        names = [r["full_name"] for r in pub["feed"].get("swe|build|", [])]
        self.assertIn("good/a", names)
        self.assertNotIn("bad/b", names)          # failed screening
        self.assertNotIn("stale/c", names)        # older than 30 days
        self.assertNotIn("unscreened/d", names)   # no record

    def test_expired_record_excluded(self):
        keys = {"swe|build|": [frow("exp/a")]}
        pub = self.build(keys, {"exp/a": rec("exp/a", expires_days=-1)})
        self.assertEqual(pub["feed"].get("swe|build|", []), [])

    def test_quarantined_repo_never_published_even_if_recorded_pass(self):
        keys = {"cyber|build|": [frow("ElementTrail/Multichain-Drainer")]}
        pub = self.build(keys, {"ElementTrail/Multichain-Drainer": rec("ElementTrail/Multichain-Drainer")})
        self.assertEqual(pub["feed"].get("cyber|build|", []), [])

    def test_every_published_row_carries_screened_block_and_label(self):
        keys = {"data|build|": [frow("good/a")]}
        pub = self.build(keys, {"good/a": rec("good/a")})
        r = pub["feed"]["data|build|"][0]
        self.assertIn("screened", r)
        self.assertEqual(r["screened"]["sha"], "a" * 40)
        self.assertEqual(pub["meta"]["label"], gate.LABEL)
        self.assertIn("fallback", pub)  # synthetic self-contained ideas present

    def test_hackathon_with_bad_link_dropped(self):
        idx = {"keys": {}, "static": {"starters": {}, "cyber_domains": {}, "cases": {}, "paths": {}, "stages": []},
               "hackathons": {"at": self.meta["generated_at"], "items": [
                   {"title": "Good", "url": "https://devpost.com/x", "when": "", "where": "NYC", "org": "", "src": "Devpost"},
                   {"title": "Bad", "url": "https://mediafire.com/evil.exe", "when": "", "where": "NYC", "org": "", "src": "x"}]}}
        pub = pipeline._build_published(idx, {"records": {}}, self.meta, self.quar)
        urls = [h["url"] for h in pub["hackathons"]["items"]]
        self.assertIn("https://devpost.com/x", urls)
        self.assertNotIn("https://mediafire.com/evil.exe", urls)


class Drop(unittest.TestCase):
    def test_drop_excludes_already_seen(self):
        meta = pipeline._meta("operational")
        published = {"feed": {"swe|build|": [
            {**frow("new/a"), "kind": "fresh", "screened": {"sha": "a" * 40}, "level": "beginner"},
            {**frow("old/b"), "kind": "fresh", "screened": {"sha": "a" * 40}, "level": "beginner"}]},
            "hackathons": {"items": []}, "evergreen": {}}
        seen = {"old/b": "2026-01-01"}
        drop = pipeline._drop_sections(published, seen)
        picked = [r["full_name"] for sec in drop for part in sec.get("sections", []) for r in part["rows"]]
        self.assertIn("new/a", picked)
        self.assertNotIn("old/b", picked)


class Status(unittest.TestCase):
    def test_status_text_mentions_pause_when_not_operational(self):
        # with no published.json present in cwd during test, status is not operational
        txt = gate.status_text()
        self.assertTrue("OPERATIONAL" in txt or "PAUSED" in txt)
        self.assertIn(gate.LABEL, txt)


class SyntheticNote(unittest.TestCase):
    """The synthetic idea is one fixed string per major, so it must not repost every run."""

    def test_note_is_suppressed_inside_the_cooldown_and_allowed_after(self):
        import datetime
        today = datetime.date.today()
        fresh = {"note:data": (today - datetime.timedelta(days=1)).isoformat()}
        stale = {"note:data": (today - datetime.timedelta(days=pipeline.NOTE_COOLDOWN_DAYS + 1)).isoformat()}
        self.assertTrue(pipeline._note_recent(fresh, "data"))
        self.assertFalse(pipeline._note_recent(stale, "data"))
        self.assertFalse(pipeline._note_recent({}, "data"))  # never posted


if __name__ == "__main__":
    unittest.main()


class ProjectLadder(unittest.TestCase):
    """The curated Python + SQL ladder is screened like every other repo — no shortcut for being hand-picked."""

    def setUp(self):
        self.meta = pipeline._meta("operational")
        self.quar = gate.load_quarantine()

    def build(self, records):
        item = lambda name, lv="start": {"level": lv, "lang": "Python", "name": name, "todo": "do the thing",
                                         "repo": frow(name) | {"kind": "evergreen"}}
        idx = {"keys": {}, "static": {"starters": {}, "cyber_domains": {}, "cases": {}, "paths": {},
                                      "projects": {"cyber": [item("good/a"), item("bad/b", "deep")]}, "stages": []},
               "hackathons": {"at": self.meta["generated_at"], "items": []}}
        return pipeline._build_published(idx, {"policy_version": gate.POLICY_VERSION, "records": records}, self.meta, self.quar)

    def test_a_failing_project_repo_is_dropped_task_and_all(self):
        pub = self.build({"good/a": rec("good/a"), "bad/b": rec("bad/b", result="fail")})
        names = [it["repo"]["full_name"] for it in pub["evergreen"]["projects"]["cyber"]]
        self.assertEqual(names, ["good/a"])

    def test_validator_checks_project_repos_too(self):
        pub = self.build({"good/a": rec("good/a")})
        pub["evergreen"]["projects"]["cyber"].append({"level": "deep", "lang": "SQL", "name": "sneaky/x",
                                                      "todo": "x", "repo": frow("sneaky/x") | {"kind": "evergreen"}})
        ok, problems = pipeline.validate_published(pub, {"records": {"good/a": rec("good/a")}}, self.quar, self.meta)
        self.assertFalse(ok)
        self.assertTrue(any("sneaky/x" in p for p in problems), problems)

    def test_every_catalogue_entry_is_well_formed(self):
        import project_scout as ps
        for major, entries in ps.PROJECTS.items():
            self.assertIn(major, ("cyber", "swe", "data"))
            for level, lang, name, todo in entries:
                self.assertIn(level, ps.LEVELS3)
                self.assertIn(lang, ("Python", "SQL"))
                self.assertRegex(name, r"^[\w.-]+/[\w.-]+$")
                self.assertGreater(len(todo), 40)          # a task, not a label
            self.assertTrue({e[0] for e in entries} == set(ps.LEVELS3), major)   # all three levels present
            self.assertTrue({e[1] for e in entries} == {"Python", "SQL"}, major)  # both languages present


class ToMarkdown(unittest.TestCase):
    """Every Telegram tag the feed emits must become Discord markdown, never escaped literal HTML."""

    def test_code_becomes_a_backtick_span(self):
        import project_scout as ps
        out = ps.to_markdown("Need first: <code>pip install transformers torch</code>")
        self.assertEqual(out, "Need first: `pip install transformers torch`")

    def test_code_span_is_not_markdown_escaped_inside(self):
        import project_scout as ps
        out = ps.to_markdown("<code>pip install my_pkg[all] &amp;&amp; ls</code>")
        self.assertEqual(out, "`pip install my_pkg[all] && ls`")

    def test_untagged_angle_brackets_still_escaped(self):
        import project_scout as ps
        self.assertIn(r"\<script\>", ps.to_markdown("&lt;script&gt;"))
