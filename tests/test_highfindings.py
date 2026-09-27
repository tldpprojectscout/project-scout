"""Tests for the four HIGH-priority review findings. Offline: engines and network are mocked, no repo is cloned."""
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import gate
import pipeline
import scan

# Relative to the real clock, NOT a hardcoded date. validate_published() takes no
# `now` argument, so gate.eligible() inside it reads datetime.now() — a frozen NOW
# here meant the fixtures aged out in real time and the suite went red on its own.
# It did, on 2026-09-27: records built as NOW + 10 days expired and CI (digest.yml,
# weekly.yml run `python -m unittest` as a blocking step) failed before publishing.
# test_gate.py can keep its fixed date because it passes NOW explicitly on every call.
NOW = datetime.now(timezone.utc)
SHA = "a" * 40
FRESH = (NOW - timedelta(days=2)).date().isoformat()


# ---- HIGH 1: required scanners fail closed --------------------------------------------------------------------------
class RunnerStatuses(unittest.TestCase):
    def test_clamav_missing_binary_unavailable(self):
        with mock.patch.object(scan.shutil, "which", return_value=None):
            self.assertEqual(scan.run_clamav("d", {}), ("unavailable", []))

    def test_clamav_db_error_is_error(self):
        r = types.SimpleNamespace(returncode=2, stdout="")
        with mock.patch.object(scan.shutil, "which", return_value="/usr/bin/clamscan"), \
             mock.patch.object(scan.subprocess, "run", return_value=r):
            self.assertEqual(scan.run_clamav("d", {}), ("error", []))

    def test_clamav_timeout(self):
        with mock.patch.object(scan.shutil, "which", return_value="/usr/bin/clamscan"), \
             mock.patch.object(scan.subprocess, "run", side_effect=scan.subprocess.TimeoutExpired("clamscan", 1)):
            self.assertEqual(scan.run_clamav("d", {}), ("timeout", []))

    def test_clamav_found_is_ran_with_hits(self):
        r = types.SimpleNamespace(returncode=1, stdout="/d/bad.exe: Win.Trojan.X FOUND\n")
        with mock.patch.object(scan.shutil, "which", return_value="/usr/bin/clamscan"), \
             mock.patch.object(scan.subprocess, "run", return_value=r):
            status, hits = scan.run_clamav("/d", {})
            self.assertEqual(status, "ran")
            self.assertTrue(hits)

    def test_osv_error_code_and_malformed(self):
        with mock.patch.object(scan.shutil, "which", return_value="/usr/bin/osv-scanner"):
            with mock.patch.object(scan.subprocess, "run", return_value=types.SimpleNamespace(returncode=127, stdout="")):
                self.assertEqual(scan.run_osv("d", {})[0], "error")
            with mock.patch.object(scan.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout="not json")):
                self.assertEqual(scan.run_osv("d", {})[0], "error")
            with mock.patch.object(scan.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout='{"no_results": 1}')):
                self.assertEqual(scan.run_osv("d", {})[0], "error")


def _fake_scan_env(semgrep_data=None):
    """Patch scan_repo's internals so it runs offline with a fake clone and controllable engines."""
    d = tempfile.mkdtemp()
    data = semgrep_data or {"results": [], "paths": {"scanned": ["a.py"], "skipped": []}, "errors": []}
    return mock.patch.multiple(
        scan,
        pin_sha=mock.Mock(return_value=SHA), clone=mock.Mock(return_value=d), head_sha=mock.Mock(return_value=SHA),
        prepare_tree=mock.Mock(return_value={"bytes": 100, "files": 1}),
        run_semgrep=mock.Mock(return_value=(data, None)),
        semgrep_version=mock.Mock(return_value="1.177.0"),
    ), d


class ScanVerdict(unittest.TestCase):
    def _run(self, y, c, o, semgrep_data=None):
        patch, _ = _fake_scan_env(semgrep_data)
        with patch, mock.patch.object(scan.shutil, "which", return_value="/usr/bin/x"), \
             mock.patch.object(scan, "run_yara", return_value=y), \
             mock.patch.object(scan, "run_clamav", return_value=c), \
             mock.patch.object(scan, "run_osv", return_value=o):
            return scan.scan_repo("o/r", 100, "Python", sha=SHA)

    def test_all_engines_ran_clean_passes(self):
        r = self._run(("ran", []), ("ran", []), ("ran", ([], 0)))
        self.assertEqual(r["result"], "pass")

    def test_missing_engine_is_incomplete_not_pass(self):
        r = self._run(("ran", []), ("unavailable", []), ("ran", ([], 0)))
        self.assertEqual(r["result"], "incomplete")
        self.assertTrue(any("clamav" in x for x in r["reasons"]))

    def test_engine_error_is_incomplete(self):
        r = self._run(("ran", []), ("error", []), ("ran", ([], 0)))
        self.assertEqual(r["result"], "incomplete")

    def test_a_hit_withholds_even_if_another_engine_failed(self):
        r = self._run(("ran", ["TLDP_X@f"]), ("unavailable", []), ("ran", ([], 0)))
        self.assertEqual(r["result"], "withheld")

    def test_zero_scanned_code_files_is_incomplete(self):
        data = {"results": [], "paths": {"scanned": [], "skipped": []}, "errors": []}
        r = self._run(("ran", []), ("ran", []), ("ran", ([], 0)), semgrep_data=data)
        self.assertEqual(r["result"], "incomplete")  # Python repo, zero files scanned = no coverage


# ---- HIGH 2 + 3: commit integrity and README/link screening through screen_one --------------------------------------
class FakeScanMod:
    def __init__(self, result="pass", sha=SHA, reasons=None, engines=None):
        self.result, self.sha, self.reasons = result, sha, reasons or []
        self.engines = engines or {"semgrep": "ran", "yara": "ran", "clamav": "ran", "osv": "ran"}
        self.calls = {}

    def semgrep_version(self): return "1.177.0"
    def ruleset_sha256(self): return "abc"

    def scan_repo(self, full, size=None, language=None, sha=None):
        self.calls["scan_sha"] = sha
        return {"sha": self.sha, "result": self.result, "reasons": self.reasons,
                "coverage": {"code_scan_result": self.result, "engines": self.engines},
                "semgrep": {"blocking": [], "advisory": {}}, "yara": {}, "clamav": {}, "osv": {}}


class FakeVetMod:
    def __init__(self, ok=True, hard=None, links=None):
        self.ok, self.hard, self.links = ok, hard or [], links or {"block": [], "warn": [], "error": None}
        self.calls = {}

    def vet(self, full, name_counts=None, ref=None):
        self.calls["ref"] = ref
        return {"ok": self.ok, "hard": self.hard, "soft": [], "links": self.links}


QUAR = {"names": set(), "reasons": {}, "patterns": []}
ROW = {"full_name": "o/r", "language": "Python", "size": 100, "stargazers_count": 5, "pushed_at": FRESH, "created_at": FRESH}


class CommitAndLinks(unittest.TestCase):
    def test_screen_one_pins_same_sha_to_vet_and_scan(self):
        sm, vm = FakeScanMod(), FakeVetMod()
        rec = gate.screen_one(ROW, QUAR, sha=SHA, vet_mod=vm, scan_mod=sm)
        self.assertEqual(rec["result"], "pass")
        self.assertEqual(rec["sha"], SHA)
        self.assertEqual(vm.calls["ref"], SHA)   # deep vet read the reviewed commit
        self.assertEqual(sm.calls["scan_sha"], SHA)  # code scan read the reviewed commit

    def test_unreachable_sha_is_not_reused(self):
        # no sha given and head_sha returns None -> cannot confirm the commit -> None (retry, no eligibility)
        with mock.patch.object(gate, "head_sha", return_value=None):
            self.assertIsNone(gate.screen_one(ROW, QUAR, vet_mod=FakeVetMod(), scan_mod=FakeScanMod()))

    def test_suspicious_link_blocks_publication(self):
        vm = FakeVetMod(ok=False, hard=["README link: file-sharing host mediafire.com"],
                        links={"block": ["file-sharing host mediafire.com"], "warn": [], "error": None})
        rec = gate.screen_one(ROW, QUAR, sha=SHA, vet_mod=vm, scan_mod=FakeScanMod())
        self.assertEqual(rec["result"], "fail")
        self.assertTrue(any("mediafire" in r for r in rec["reasons"]))

    def test_unsafe_install_instruction_blocks(self):
        vm = FakeVetMod(ok=False, hard=["README link: instructs to disable security protection"],
                        links={"block": ["instructs to disable security protection"], "warn": [], "error": None})
        rec = gate.screen_one(ROW, QUAR, sha=SHA, vet_mod=vm, scan_mod=FakeScanMod())
        self.assertEqual(rec["result"], "fail")

    def test_unreadable_readme_is_incomplete_not_pass(self):
        vm = FakeVetMod(ok=True, links={"block": [], "warn": [], "error": "500"})
        rec = gate.screen_one(ROW, QUAR, sha=SHA, vet_mod=vm, scan_mod=FakeScanMod())
        self.assertEqual(rec["result"], "incomplete")


# ---- HIGH 4: independent validation of the published snapshot --------------------------------------------------------
def frow(full, sha=SHA, kind="fresh", pushed=FRESH):
    return {"full_name": full, "html_url": f"https://github.com/{full}", "stargazers_count": 5, "language": "Python",
            "description": "x", "size": 100, "pushed_at": pushed, "created_at": pushed, "kind": kind,
            "screened": {"sha": sha, "expires": (NOW + timedelta(days=10)).isoformat()}}


def store_of(*recs):
    return {"policy_version": gate.POLICY_VERSION, "generated_at": NOW.isoformat(),
            "records": {r["full_name"]: r for r in recs}}


def good_rec(full, sha=SHA):
    return {"full_name": full, "result": "pass", "sha": sha, "policy_version": gate.POLICY_VERSION,
            "at": NOW.isoformat(), "expires": (NOW + timedelta(days=10)).isoformat(),
            "coverage": {"code_scan_result": "pass", "engines": {"semgrep": "ran", "yara": "ran", "clamav": "ran", "osv": "ran"}}}


class SnapshotValidation(unittest.TestCase):
    def setUp(self):
        self.meta = pipeline._meta("operational")
        self.meta["last_screening_at"] = NOW.isoformat()
        self.quar = gate.load_quarantine()

    def pub(self, feed):
        return {"meta": self.meta, "feed": feed, "evergreen": {"starters": {}, "cyber_domains": {}, "cases": {}, "paths": {}, "stages": []},
                "hackathons": {"items": []}}

    def test_clean_snapshot_validates(self):
        pub = self.pub({"swe|build|": [frow("a/b")]})
        ok, problems = pipeline.validate_published(pub, store_of(good_rec("a/b")), self.quar, self.meta)
        self.assertTrue(ok, problems)

    def test_tampered_sha_rejected(self):
        # a row claims a screened sha that does not match the authoritative record
        pub = self.pub({"swe|build|": [frow("a/b", sha="b" * 40)]})
        ok, problems = pipeline.validate_published(pub, store_of(good_rec("a/b", sha="a" * 40)), self.quar, self.meta)
        self.assertFalse(ok)

    def test_missing_engine_record_rejected(self):
        rec = good_rec("a/b")
        rec["coverage"]["engines"]["osv"] = "unavailable"
        ok, _ = pipeline.validate_published(self.pub({"swe|build|": [frow("a/b")]}), store_of(rec), self.quar, self.meta)
        self.assertFalse(ok)

    def test_quarantined_row_rejected(self):
        q = {"names": {"a/b"}, "reasons": {"a/b": "bad"}, "patterns": []}
        ok, _ = pipeline.validate_published(self.pub({"swe|build|": [frow("a/b")]}), store_of(good_rec("a/b")), q, self.meta)
        self.assertFalse(ok)

    def test_evergreen_row_validated_too(self):
        pub = self.pub({})
        pub["evergreen"]["starters"] = {"swe": [frow("a/b", kind="evergreen")]}
        ok, _ = pipeline.validate_published(pub, store_of(good_rec("a/b")), self.quar, self.meta)
        self.assertTrue(ok)
        # break the evergreen record -> whole snapshot invalid
        rec = good_rec("a/b"); rec["result"] = "fail"
        ok2, _ = pipeline.validate_published(pub, store_of(rec), self.quar, self.meta)
        self.assertFalse(ok2)

    def test_expired_record_rejected(self):
        rec = good_rec("a/b"); rec["expires"] = (NOW - timedelta(days=1)).isoformat()
        ok, _ = pipeline.validate_published(self.pub({"swe|build|": [frow("a/b")]}), store_of(rec), self.quar, self.meta)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
