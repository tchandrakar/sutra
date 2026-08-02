"""Functional test suite for the Tier-3 backend (org_api.py + app.py).

stdlib only: unittest + urllib.request + subprocess. No httpx/requests/
fastapi.testclient (not in requirements.txt). Spawns a real uvicorn
subprocess per TestCase class, bound to 127.0.0.1 on a free port, with
SUTRA_NATIVE_HOME pointed at a FRESH tempfile.mkdtemp() re-seeded via
fixture_seed.seed() -- NEVER ~/.sutra-native and NEVER the shared
scratchpad/dev-registry (that one is for manual/dev use only).

Run: .venv/bin/python -m unittest test_app -v   (from inside sutra-ui/)
"""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
VENV_PY = os.path.join(HERE, ".venv", "bin", "python")


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http(method, path, body=None, timeout=5):
    url = "http://127.0.0.1:%d%s" % (TestApp.port, path)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                  headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _get(path):
    return _http("GET", path)


def _post(path, body):
    return _http("POST", path, body)


class TestApp(unittest.TestCase):
    """One shared server for the whole class -- read-mostly endpoints don't
    need per-test isolation, but the registry dir is fresh for THIS run only
    (never reused across runs, never the live one)."""

    proc = None
    port = None
    tmpdir = None
    userdir = None      # stand-in for ~/.sutra-ui (settings + drafts)
    _env_saved = None

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="sutra-test-app-")
        sys.path.insert(0, os.path.join(HERE, "..", "lib"))
        # seed BEFORE the server imports placement_engine in the subprocess --
        # the subprocess does its own import, so we just need the files on
        # disk before it starts.
        sys.path.insert(0, HERE)
        import fixture_seed  # noqa: E402
        fixture_seed.seed(cls.tmpdir)

        # ---- the OPERATOR's files are not test fixtures --------------------
        # providers.SETTINGS_PATH and org_api.DRAFTS_DIR default to the REAL
        # ~/.sutra-ui. POST /api/settings and POST /api/providers/active WRITE
        # there, so an unisolated run would rewrite the operator's chosen
        # provider / permission_mode as a side effect of `python -m unittest`,
        # and test_19 would edit their real draft. Point both at a tempdir --
        # in THIS process (test_19/test_20/test_43 import org_api and providers
        # directly, and both resolve the path at import time) and in the server
        # subprocess, so the two agree on which file is under test.
        #
        # Deliberately NOT under cls.tmpdir: test_20 and test_43 assert these
        # land OUTSIDE SUTRA_NATIVE_HOME, and nesting them inside it would make
        # that assertion pass vacuously.
        cls.userdir = tempfile.mkdtemp(prefix="sutra-test-userdir-")
        cls._env_saved = {k: os.environ.get(k)
                          for k in ("SUTRA_UI_SETTINGS", "SUTRA_UI_DRAFTS",
                                    "SUTRA_UI_PROVIDER", "SUTRA_UI_PERMISSION_MODE",
                                    "SUTRA_UI_WORKDIR_ROOT")}
        os.environ["SUTRA_UI_SETTINGS"] = os.path.join(cls.userdir, "settings.json")
        os.environ["SUTRA_UI_DRAFTS"] = os.path.join(cls.userdir, "drafts")
        # workdir is confined to $HOME by default (it becomes the spawned
        # agent's cwd, so an arbitrary path is a read oracle). These tests use
        # a tempdir, which is outside $HOME on macOS -- widen the root the same
        # way an operator would when starting the server.
        os.environ["SUTRA_UI_WORKDIR_ROOT"] = cls.userdir
        # An env override outranks settings.json in active_provider_detail(),
        # so leaving these set would make the provider/settings tests assert
        # the developer's shell config instead of the code under test.
        os.environ.pop("SUTRA_UI_PROVIDER", None)
        os.environ.pop("SUTRA_UI_PERMISSION_MODE", None)

        cls.port = _free_port()
        env = dict(os.environ)
        env["SUTRA_NATIVE_HOME"] = cls.tmpdir
        env.pop("SUTRA_UI_PERMISSION_MODE", None)  # exercise the real default
        cls.proc = subprocess.Popen(
            [VENV_PY, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
             "--port", str(cls.port), "--log-level", "warning"],
            cwd=HERE, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 10
        last_err = None
        while time.time() < deadline:
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/api/org/stats" % cls.port, timeout=1)
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(0.25)
        else:
            out = cls.proc.stdout.read().decode("utf-8", "replace") if cls.proc.stdout else ""
            raise RuntimeError("server did not come up: %r\n%s" % (last_err, out))

    @classmethod
    def tearDownClass(cls):
        if cls.proc:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
                cls.proc.wait(timeout=5)
        if cls.tmpdir and os.path.isdir(cls.tmpdir):
            shutil.rmtree(cls.tmpdir, ignore_errors=True)
        if cls.userdir and os.path.isdir(cls.userdir):
            shutil.rmtree(cls.userdir, ignore_errors=True)
        for k, v in (cls._env_saved or {}).items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ---------------------------------------------------------------- tree -

    def test_01_tree_shape_and_count(self):
        status, rows = _get("/api/org/tree")
        self.assertEqual(status, 200)
        self.assertGreater(len(rows), 0)
        for r in rows:
            for field in ("ref", "path", "name", "status", "parent_ref",
                           "tenant_id", "mint_evidence", "ts_minted_ms",
                           "successor_refs"):
                self.assertIn(field, r, "tree row missing %r: %r" % (field, r))
        # live, T-local scope by default -- one retired T-local domain and the
        # T-acme root must both be absent
        self.assertTrue(all(row["status"] != "retired" for row in rows))
        self.assertTrue(all(row["tenant_id"] == "T-local" for row in rows))

    def test_02_tree_include_retired_adds_the_tombstone(self):
        _, live_rows = _get("/api/org/tree")
        _, all_rows = _get("/api/org/tree?include_retired=true")
        self.assertGreater(len(all_rows), len(live_rows))
        self.assertTrue(any(r["status"] == "retired" for r in all_rows))

    def test_03_tree_all_tenants_adds_the_leak(self):
        _, scoped = _get("/api/org/tree?all_tenants=false")
        _, everyone = _get("/api/org/tree?all_tenants=true")
        self.assertGreater(len(everyone), len(scoped))
        self.assertTrue(any(r["tenant_id"] != "T-local" for r in everyone))

    def test_04_dpaths_are_unique_per_tenant(self):
        # D-numbering is PER TENANT TREE, not global -- T-local's "Sutra Labs"
        # and T-acme's "Client Success" are each their own root and each
        # legitimately gets "D0". Uniqueness only holds within one tenant.
        _, rows = _get("/api/org/tree?include_retired=true&all_tenants=true")
        by_tenant = {}
        for r in rows:
            by_tenant.setdefault(r["tenant_id"], []).append(r["path"])
        self.assertGreaterEqual(len(by_tenant), 2, "expected at least T-local + T-acme")
        for tenant_id, paths in by_tenant.items():
            self.assertEqual(len(paths), len(set(paths)),
                              "duplicate D-paths within tenant %r: %r" % (tenant_id, paths))
        root = [r for r in rows if r["parent_ref"] is None]
        self.assertGreaterEqual(len(root), 2)  # Sutra Labs D0 + Client Success D0

    # ------------------------------------------------------------- stats ---

    def test_05_stats_matches_tree(self):
        status, stats = _get("/api/org/stats")
        self.assertEqual(status, 200)
        for k in ("domains", "domains_live", "domains_retired", "charters",
                   "placements", "current_rows", "confidence_floor"):
            self.assertIn(k, stats)
        self.assertEqual(stats["domains"], stats["domains_live"] + stats["domains_retired"])
        self.assertAlmostEqual(stats["confidence_floor"], 0.45, places=2)
        _, all_rows = _get("/api/org/tree?include_retired=true&all_tenants=true")
        self.assertEqual(stats["domains"], len(all_rows))

    # ---------------------------------------------------------- charters --

    def test_05b_root_serves_the_studio_not_the_legacy_console(self):
        """Regression: "/" used to serve term.html (the xterm console), so the
        front door showed a completely different UI from the studio and the
        operator reported "the UI does not match" -- correctly. "/" must serve
        the studio, and it must be the same bytes as /panel."""
        def _raw(path):
            req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path))
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode("utf-8", "replace")
        s_root, root = _raw("/")
        s_panel, panel = _raw("/panel")
        self.assertEqual(s_root, 200)
        self.assertEqual(s_panel, 200)
        self.assertEqual(root, panel, "/ and /panel must serve identical bytes")
        # positively the studio, not the console
        self.assertIn("<title>Sutra</title>", root)
        self.assertIn('id="panes"', root)
        self.assertNotIn("xterm", root.lower())
        # and the legacy console is still reachable, just not on the front door
        s_legacy, legacy = _raw("/legacy/term")
        self.assertEqual(s_legacy, 200)
        self.assertIn("xterm", legacy.lower())

    def test_06b_charters_include_those_on_retired_domains(self):
        """Regression: scoping /api/org/charters to live_refs() hid the charter
        stranded on the retired 'Agent Ops' tombstone -- the exact ORG-002
        defect the Charters screen exists to surface (it renders a 'tombstone'
        pill on the owner cell). Scope by TENANT, never by liveness."""
        _, charters = _get("/api/org/charters?all_tenants=true")
        _, tree = _get("/api/org/tree?include_retired=true&all_tenants=true")
        retired_refs = {d["ref"] for d in tree if d["status"] == "retired"}
        self.assertTrue(retired_refs, "fixture must seed a retired domain")
        orphaned = [c for c in charters if c["domain_ref"] in retired_refs]
        self.assertTrue(orphaned,
            "a charter homed to a retired domain must still be returned -- "
            "hiding it hides the defect the screen exists to show")
        # and the count must equal every charter body on disk for that scope
        _, stats = _get("/api/org/stats")
        self.assertEqual(len(charters), stats["charters"],
            "charters endpoint must not silently drop rows vs the on-disk count")

    def test_06_charters_shape(self):
        status, rows = _get("/api/org/charters")
        self.assertEqual(status, 200)
        self.assertGreater(len(rows), 0)
        for c in rows:
            for field in ("id", "title", "purpose", "domain_ref"):
                self.assertIn(field, c)
        ids = [c["id"] for c in rows]
        self.assertEqual(len(ids), len(set(ids)), "duplicate charter ids returned")

    # -------------------------------------------------------- placements --

    def test_07_placements_have_derived_mode(self):
        status, rows = _get("/api/org/placements")
        self.assertEqual(status, 200)
        self.assertGreater(len(rows), 0)
        for p in rows:
            self.assertIn("mode", p)
            self.assertIn(p["mode"], ("match", "floor"))
            floor = p["mode"] == "floor"
            below = p["confidence"] < 0.45
            self.assertEqual(floor, below,
                              "mode/confidence disagree: %r" % p)
        # newest-first
        ts = [p["ts_ms"] for p in rows]
        self.assertEqual(ts, sorted(ts, reverse=True))

    def test_08_placements_all_tenants_includes_acme(self):
        _, scoped = _get("/api/org/placements")
        _, everyone = _get("/api/org/placements?all_tenants=true")
        self.assertGreaterEqual(len(everyone), len(scoped))

    # ------------------------------------------------------------ health --

    def test_09_health_surfaces_seeded_defects(self):
        status, health = _get("/api/org/health")
        self.assertEqual(status, 200)
        self.assertIn("mece", health)
        self.assertIn("lint", health)

    # ----------------------------------------------------------- history --

    def test_09b_history_returns_events_newest_first(self):
        status, h = _get("/api/org/history")
        self.assertEqual(status, 200)
        self.assertIn("events", h)
        self.assertIn("meta", h)
        self.assertGreater(len(h["events"]), 0)
        ts = [e.get("ts_ms") or 0 for e in h["events"]]
        self.assertEqual(ts, sorted(ts, reverse=True), "history must be newest-first")

    def test_09c_history_completeness_is_marked_derived(self):
        _, h = _get("/api/org/history")
        m = h["meta"]
        # history_complete_from_ms is NOT a stored engine field -- it is derived
        # from the earliest before/after-carrying event. It must say so, or a
        # caller will treat it as authoritative stored state.
        self.assertIs(m["derived"], True)
        self.assertEqual(m["domain_index_lines"], len(h["events"]))
        # enriched + events_without_before partitions EVERY row. legacy_events
        # is the narrower count (restructures only) the timeline greys out --
        # see test_09d -- so it is a subset, never the complement.
        self.assertEqual(m["enriched_events"] + m["events_without_before"], len(h["events"]))
        self.assertLessEqual(m["legacy_events"], m["events_without_before"])
        if m["enriched_events"]:
            enriched_ts = [e["ts_ms"] for e in h["events"] if e.get("before") is not None]
            self.assertEqual(m["history_complete_from_ms"], min(enriched_ts))
        else:
            self.assertIsNone(m["history_complete_from_ms"])

    def test_09d_history_legacy_rows_lack_before_snapshots(self):
        """`legacy_events` is rendered as "N legacy row(s) below the line"
        directly under the timeline, so it must count the rows the timeline
        actually greys out: `event == "domain_restructured" && !before`. A
        MINT has no `before` because there was no prior state, not because a
        snapshot was lost -- counting mints as legacy printed 5 under a
        timeline showing 1 greyed row."""
        _, h = _get("/api/org/history")
        greyed = [e for e in h["events"]
                  if e.get("event") == "domain_restructured" and e.get("before") is None]
        self.assertEqual(len(greyed), h["meta"]["legacy_events"])
        no_before = [e for e in h["events"] if e.get("before") is None]
        self.assertEqual(len(no_before), h["meta"]["events_without_before"])
        # the fixture seeds both shapes, so this is a real distinction here
        self.assertLess(len(greyed), len(no_before))

    # --------------------------------------------------------- classify ---

    def _placement_file_count(self):
        # count via the stats endpoint rather than touching the filesystem
        # directly, so the assertion exercises the same code path a real
        # client would see.
        return _get("/api/org/stats")[1]["placements"]

    def test_10_classify_dense_evidence_match_clears_the_real_floor(self):
        # NOTE: the real engine's confidence formula (score_domains' saturating
        # absolute term + margin-over-runner-up, placement_engine.py's
        # classify()) is NOT the earlier client-side Jaccard approximation --
        # verified directly against this fixture: "Draft the quarterly report
        # for Acme" scores 0.2372 (floor) against the real engine, not the
        # earlier ~0.51 "match". Dense, evidence-term-heavy phrasing is what
        # clears the real 0.45 floor; ordinary task phrasing mostly doesn't
        # with this fixture's sparse (5-7 term) mint_evidence lists. This
        # test proves match-mode is genuinely reachable end-to-end.
        before = self._placement_file_count()
        status, r = _post("/api/classify",
                           {"text": "terraform runbook backup restore infrastructure"})
        self.assertEqual(status, 200)
        self.assertEqual(r.get("mode"), "match")
        self.assertGreaterEqual(r["confidence"], 0.45)
        self.assertEqual(r.get("domain_name"), "Infrastructure")
        self.assertIn("placement", r)
        after = self._placement_file_count()
        self.assertEqual(after, before + 1, "classify must write EXACTLY one placement")

    def test_11_classify_ordinary_phrasing_holds_at_ancestor(self):
        # "Draft the quarterly report for Acme" is exactly the kind of
        # ordinary, on-topic request an operator would actually type -- and
        # against the real engine + this sparse fixture it holds at the
        # floor. That is the honest, demonstrated behavior of I-P9, not a
        # test artifact: a run of these is the signal the org is missing a
        # department, per §3.6(c) of the design doc.
        before = self._placement_file_count()
        status, r = _post("/api/classify", {"text": "Draft the quarterly report for Acme"})
        self.assertEqual(status, 200)
        self.assertEqual(r.get("mode"), "floor")
        self.assertLess(r["confidence"], 0.45)
        self.assertIn("held_at_ref", r)
        after = self._placement_file_count()
        # floor still resolves to an ancestor with a charter (root always has
        # one) and writes a row -- floor is "held", not "refused".
        self.assertEqual(after, before + 1)

    def test_11b_classify_no_candidate_at_all_is_mode_none(self):
        before = self._placement_file_count()
        status, r = _post("/api/classify",
                           {"text": "Negotiate the office lease renewal in Berlin"})
        self.assertEqual(status, 200)
        self.assertEqual(r.get("mode"), "none")
        self.assertIn("blocked", r)
        after = self._placement_file_count()
        self.assertEqual(after, before, "mode=none must never write a placement")

    def test_12_classify_never_mints_a_domain(self):
        before_domains = _get("/api/org/stats")[1]["domains"]
        texts = [
            "xyzzy plugh quux never seen before",
            "the quarterly acme report",
            "terraform runbook backup restore",
            "negotiate berlin office lease",
            "totally unrelated gibberish request zzz",
        ]
        for t in texts:
            status, _ = _post("/api/classify", {"text": t})
            self.assertEqual(status, 200)
        after_domains = _get("/api/org/stats")[1]["domains"]
        self.assertEqual(before_domains, after_domains,
                          "classify() must never mint a domain (resolve() would; we forbid it)")

    def test_13_classify_empty_text_is_400_not_500(self):
        status, r = _post("/api/classify", {"text": "   "})
        self.assertEqual(status, 400)

    def test_14_classify_concurrent_calls_do_not_corrupt_current_jsonl(self):
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(
                lambda i: _post("/api/classify", {"text": "concurrent test task %d" % i}),
                range(5)))
        for status, r in results:
            self.assertEqual(status, 200)
        status, rows = _get("/api/org/placements")
        self.assertEqual(status, 200)
        ids = [p["id"] for p in rows]
        self.assertEqual(len(ids), len(set(ids)), "duplicate placement ids under concurrency")

    # ---------------------------------------------------------- simulate --

    def test_15_simulate_empty_ops_surfaces_seeded_defects(self):
        status, sim = _post("/api/org/simulate", {"ops": []})
        self.assertEqual(status, 200)
        codes = {f["code"] for f in sim["findings"]}
        self.assertIn("ORG-002", codes, "seeded tombstone charter should fire ORG-002")
        self.assertIn("ORG-001", codes, "Market Intel <-> Synthesis overlap should fire ORG-001")
        self.assertIn("ORG-008", codes, "charterless departments should fire ORG-008")
        self.assertTrue(len(sim.get("not_checked", [])) > 0)
        nc_codes = {n["code"] for n in sim["not_checked"]}
        for forbidden in ("ORG-005", "ORG-012", "ORG-019"):
            self.assertIn(forbidden, nc_codes)

    def test_16_simulate_ancestor_pair_is_exempt_from_org001(self):
        status, sim = _post("/api/org/simulate", {"ops": []})
        subjects = " ".join(f["subject"] for f in sim["findings"] if f["code"] == "ORG-001")
        self.assertNotIn("Research ↔ Market Intel", subjects)
        self.assertNotIn("Research <-> Market Intel", subjects)

    def test_17_simulate_move_creating_a_cycle_is_flagged(self):
        _, tree = _get("/api/org/tree")
        research = next(r for r in tree if r["name"] == "Research")
        child = next(r for r in tree if r["parent_ref"] == research["ref"])
        status, sim = _post("/api/org/simulate",
                             {"ops": [{"op": "move", "ref": research["ref"], "target": child["ref"]}]})
        self.assertEqual(status, 200)
        codes = {f["code"] for f in sim["findings"]}
        self.assertIn("ORG-006", codes)

    def test_18_simulate_does_not_mutate_the_registry(self):
        before = _get("/api/org/stats")[1]
        _, tree = _get("/api/org/tree")
        a, b = tree[0], tree[1]
        _post("/api/org/simulate", {"ops": [{"op": "move", "ref": a["ref"], "target": b["ref"]}]})
        after = _get("/api/org/stats")[1]
        self.assertEqual(before, after, "simulate() must be pure -- no registry mutation")

    # -------------------------------------------------------------- draft -

    def test_19_draft_roundtrip_and_forced_null_validation(self):
        # DRAFTS_DIR is a shared, persistent path by design (the dev server's
        # one real draft, analogous to ~/.sutra-ui/drafts/) -- NOT per-test
        # isolated. Back up and restore whatever was already there instead of
        # assuming an empty starting state.
        import org_api
        had_prior = org_api.DRAFT_PATH.exists()
        prior_bytes = org_api.DRAFT_PATH.read_bytes() if had_prior else None
        try:
            marker = "test-marker-%d" % int(time.time() * 1000)
            status, saved = _post("/api/org/draft", {
                "ops": [{"op": "move", "ref": "dref-x", "target": "dref-y"}],
                "rationale": marker,
                "validated_at_ms": 999999999999,  # client tries to lie
            })
            self.assertEqual(status, 200)
            self.assertIsNone(saved["validated_at_ms"], "server must force validated_at_ms null")
            self.assertEqual(saved["plan_origin"], "studio-drag")
            self.assertEqual(saved["rationale"], marker)
            self.assertEqual(len(saved["ops"]), 1)

            status, reread = _get("/api/org/draft")
            self.assertEqual(status, 200)
            self.assertEqual(reread["rationale"], marker)
            self.assertEqual(len(reread["ops"]), 1)
            self.assertIsNone(reread["validated_at_ms"])
        finally:
            if had_prior:
                org_api.DRAFT_PATH.write_bytes(prior_bytes)
            elif org_api.DRAFT_PATH.exists():
                org_api.DRAFT_PATH.unlink()

    def test_20_draft_file_lands_outside_any_sutra_native_home(self):
        import org_api
        self.assertNotIn(str(org_api.DRAFTS_DIR), self.tmpdir)
        self.assertFalse(str(org_api.DRAFT_PATH).startswith(self.tmpdir))

    # --------------------------------------------------- static / hygiene --

    def test_21_perm_mode_default_is_not_acceptedits(self):
        env = dict(os.environ)
        env.pop("SUTRA_UI_PERMISSION_MODE", None)
        out = subprocess.check_output(
            [VENV_PY, "-c", "import app; print(app.PERM_MODE)"],
            cwd=HERE, env=env, stderr=subprocess.STDOUT,
        ).decode("utf-8").strip().splitlines()[-1]
        self.assertNotEqual(out, "acceptEdits")
        self.assertEqual(out, "plan")

    def test_22_forbidden_calls_are_absent_from_new_files(self):
        # test_forbidden_calls.py already does this correctly (alias-resolved
        # `<E>.<name>(` call-site regex, so it doesn't self-match this very
        # safety docstring the way a naive substring search would). Reuse it
        # rather than duplicate a worse version.
        import test_forbidden_calls as tfc
        tfc.test_files_exist()
        tfc.test_org_api_declares_its_engine_import()
        tfc.test_no_forbidden_mutator_calls()  # raises AssertionError on violation

    def test_24_tree_orders_the_tenant_of_record_first(self):
        """D-numbering is PER TENANT, so both roots are "D0" and a sort on
        `path` alone is a tie broken by dict order -- T-acme's root could come
        out first. The panel takes `the first row with no parent_ref` as the
        create-department parent default, so that tie decided which TENANT a
        minted department landed in: the dropdown read "D0 Sutra Labs" while
        the copyable CLI string carried --parent <T-acme root>."""
        _, rows = _get("/api/org/tree?include_retired=true&all_tenants=true")
        self.assertEqual(rows[0]["tenant_id"], "T-local")
        roots = [r for r in rows if r["parent_ref"] is None]
        self.assertGreaterEqual(len(roots), 2)
        self.assertEqual(roots[0]["tenant_id"], "T-local",
                          "the tenant of record's root must lead the roots")
        # every T-local row precedes every foreign row
        tenants = [r["tenant_id"] for r in rows]
        self.assertEqual(tenants, sorted(tenants, key=lambda t: 0 if t == "T-local" else 1))
        # ...and the order is stable across reads (no dict-iteration wobble)
        _, again = _get("/api/org/tree?include_retired=true&all_tenants=true")
        self.assertEqual([r["ref"] for r in rows], [r["ref"] for r in again])

    def test_25_matched_terms_are_an_intersection_not_the_utterance(self):
        """matched_terms is rendered as "Filed to X on `a` `b`" -- a claim
        about WHY that domain won. Returning the raw utterance tokens made it
        a lie: a turn held at the root reported terms the root's mint_evidence
        does not contain."""
        _, tree = _get("/api/org/tree?all_tenants=true")
        by_ref = {r["ref"]: r for r in tree}

        # a floor hold at the root: nothing in the utterance matched the
        # winner's evidence, and [] is the honest answer
        status, held = _post("/api/classify", {"text": "Draft the quarterly report for Acme"})
        self.assertEqual(status, 200)
        self.assertEqual(held["mode"], "floor")
        root = by_ref[held["domain_ref"]]
        self.assertEqual(held["matched_terms"], [],
                          "the root's mint_evidence is %r -- none of these terms is in it"
                          % (root["mint_evidence"],))

        # a real match: every reported term is in the winner's own evidence
        status, hit = _post("/api/classify",
                             {"text": "terraform runbook backup restore infrastructure"})
        self.assertEqual(status, 200)
        self.assertEqual(hit["mode"], "match")
        won = by_ref[hit["domain_ref"]]
        allowed = set(won["mint_evidence"] or []) | set(str(won["name"]).lower().split())
        self.assertTrue(hit["matched_terms"])
        for term in hit["matched_terms"]:
            self.assertIn(term, allowed,
                           "%r is not in %r's name or mint_evidence" % (term, won["name"]))
        # and terms that were typed but are NOT the winner's evidence are gone
        self.assertNotIn("the", hit["matched_terms"])

    def test_26_simulate_measures_staleness_against_the_caller_s_clock(self):
        """reorg_sim derived "now" as max(placement.ts_ms) to stay pure, while
        the panel bands charter freshness against the browser's Date.now() --
        so ORG-009/ORG-020 and the freshness pills could contradict each other
        on the same screen. One clock, passed in."""
        _, now_default = _post("/api/org/simulate", {"ops": []})
        far_future = int(time.time() * 1000) + 400 * 86400000
        _, aged = _post("/api/org/simulate", {"ops": [], "now_ms": far_future})
        n_default = len([f for f in now_default["findings"] if f["code"] == "ORG-020"])
        n_aged = len([f for f in aged["findings"] if f["code"] == "ORG-020"])
        self.assertGreater(n_aged, n_default,
                            "a later clock must age MORE standing charters past the stale band")
        # a clock before the fixture's own placements ages nothing
        _, young = _post("/api/org/simulate", {"ops": [], "now_ms": 0})
        self.assertEqual([f for f in young["findings"] if f["code"] in ("ORG-009", "ORG-020")], [])
        # the pure default (no now_ms passed to the function) is untouched
        import reorg_sim
        import inspect
        self.assertIn("now_ms", inspect.signature(reorg_sim.simulate).parameters)
        self.assertIsNone(inspect.signature(reorg_sim.simulate).parameters["now_ms"].default)

    def test_23_panel_route_serves_the_static_file(self):
        # HTMLResponse -> json.loads (used by _http) would fail; hit it raw.
        req = urllib.request.Request("http://127.0.0.1:%d/panel" % self.port)
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            body = resp.read().decode("utf-8", "replace")
            self.assertIn("<title>", body)

    # ============================================================= new tier ==
    # The tests below extend (never replace) the ones above. Each one pins a
    # claim the panel renders verbatim to the operator, so a regression is a
    # visible lie on screen rather than an internal detail.

    def test_27_matched_terms_are_never_the_raw_utterance_tokens(self):
        """The generalisation of test_25: for EVERY utterance, whatever comes
        back as matched_terms must be a subset of the WINNING domain's own
        evidence token set (name tokens + mint_evidence), lexed by the engine's
        own tokenizer. The specific failure this pins is the one the panel
        renders: "Filed to <domain> on `a` `b`" where `a`/`b` were merely typed,
        not matched. An utterance built entirely from words no domain's
        evidence contains must therefore report [] -- and must NOT echo back
        what was typed."""
        import placement_engine as E  # the same lexer score_domains() uses
        import reorg_sim

        _, tree = _get("/api/org/tree?include_retired=true&all_tenants=true")
        by_ref = {r["ref"]: r for r in tree}

        def evidence_tokens(row):
            return {str(t).lower() for t in
                    E.tokenize(row.get("name") or "", *(row.get("mint_evidence") or []))}

        # (a) an utterance whose content words appear in NO domain's evidence,
        #     but which still resolves (held at an ancestor).
        offbeat = "Draft the quarterly report for Acme"
        status, r = _post("/api/classify", {"text": offbeat})
        self.assertEqual(status, 200)
        self.assertIsNotNone(r.get("domain_ref"))
        won = by_ref[r["domain_ref"]]
        typed = reorg_sim.tok(offbeat)
        self.assertTrue(typed, "the probe utterance must have content tokens")
        self.assertFalse(typed & evidence_tokens(won),
                          "probe is only meaningful if none of %r is in %r's evidence"
                          % (sorted(typed), won["name"]))
        self.assertEqual(r["matched_terms"], [],
                          "nothing in %r is in %r's evidence, so the only honest "
                          "answer is [] -- got %r"
                          % (offbeat, won["name"], r["matched_terms"]))

        # (b) the invariant across a spread of phrasings: always a SUBSET of the
        #     winner's evidence, never the raw utterance.
        probes = [
            "Draft the quarterly report for Acme",
            "terraform runbook backup restore infrastructure",
            "please review the onboarding checklist tomorrow",
            "fix the flaky evaluation harness for the agent",
            "the quarterly acme report",
        ]
        saw_nonempty = False
        saw_strict_subset = False
        for text in probes:
            status, res = _post("/api/classify", {"text": text})
            self.assertEqual(status, 200)
            got = res.get("matched_terms")
            self.assertIsInstance(got, list, "matched_terms must always be a list: %r" % res)
            if not res.get("domain_ref"):
                self.assertEqual(got, [], "no winner => nothing matched: %r" % res)
                continue
            allowed = evidence_tokens(by_ref[res["domain_ref"]])
            typed = reorg_sim.tok(text)
            got_set = {t.lower() for t in got}
            # the exact contract: matched_terms IS the intersection. Stronger
            # than "not the raw utterance" -- it pins both the passthrough bug
            # (got == typed when typed ⊄ evidence) and any over-filtering.
            self.assertEqual(got_set, typed & allowed,
                              "matched_terms for %r must be exactly (utterance ∩ %r's "
                              "evidence); got %r, expected %r"
                              % (text, res.get("domain_name"), sorted(got_set),
                                 sorted(typed & allowed)))
            self.assertTrue(got_set <= allowed)
            self.assertTrue(got_set <= typed,
                             "matched_terms must also be terms that were actually said")
            if got:
                saw_nonempty = True
            if got_set < typed:
                # words were typed that the winner's evidence does not contain,
                # and they were correctly dropped -- this is the passthrough bug
                # actually being exercised rather than trivially satisfied.
                saw_strict_subset = True
        self.assertTrue(saw_nonempty,
                         "at least one probe must genuinely match, or this test only "
                         "proves the endpoint returns [] unconditionally")
        self.assertTrue(saw_strict_subset,
                         "no probe typed a word outside the winner's evidence -- the "
                         "passthrough bug would not have been caught")

    def test_28_legacy_events_counts_only_restructures_without_before(self):
        """Sharper than test_09d: it is not enough that legacy_events <=
        events_without_before. The fixture seeds BOTH shapes -- restructures
        with no `before` (genuinely unreplayable) and mints with no `before`
        (nothing was lost; there was no prior state). legacy_events is printed
        as "N legacy row(s) below the line" under a timeline that greys exactly
        the first shape, so counting the second inflates the number the
        operator reads."""
        _, h = _get("/api/org/history")
        events = h["events"]
        meta = h["meta"]

        restructures_without_before = [
            e for e in events
            if e.get("event") == "domain_restructured" and e.get("before") is None]
        other_without_before = [
            e for e in events
            if e.get("event") != "domain_restructured" and e.get("before") is None]

        # the fixture must actually contain the distinguishing shape, or the
        # assertion below is vacuous and a naive count would pass too
        self.assertTrue(other_without_before,
                         "fixture must seed non-restructure events lacking `before` "
                         "(mints) for this distinction to be testable")
        self.assertTrue(restructures_without_before,
                         "fixture must seed at least one unreplayable restructure")

        self.assertEqual(meta["legacy_events"], len(restructures_without_before))
        self.assertNotEqual(meta["legacy_events"], meta["events_without_before"],
                             "legacy_events must NOT be the every-row-without-before count")
        self.assertEqual(meta["events_without_before"],
                          len(restructures_without_before) + len(other_without_before))
        # every mint is excluded by name, not by accident of ordering
        for e in other_without_before:
            self.assertNotEqual(e.get("event"), "domain_restructured")

    def test_29_tree_partitions_tenant_of_record_ahead_of_every_other_tenant(self):
        """Complements test_24 by proving the tenant key is load-bearing: a
        sort on `path` alone would INTERLEAVE the tenants here (D-numbering is
        per-tenant, so T-acme's "D0" sorts before T-local's "D1"). If the
        returned order still happens to equal the path-only order, the tenant
        key is doing nothing and the create-department parent default is one
        dict-ordering wobble away from pointing at a foreign tenant again."""
        _, rows = _get("/api/org/tree?include_retired=true&all_tenants=true")
        tenants = [r["tenant_id"] for r in rows]
        self.assertGreater(len(set(tenants)), 1, "fixture must span >1 tenant")

        first_foreign = min(i for i, t in enumerate(tenants) if t != "T-local")
        last_local = max(i for i, t in enumerate(tenants) if t == "T-local")
        self.assertLess(last_local, first_foreign,
                         "every tenant-of-record row must precede every foreign row: %r"
                         % (tenants,))

        # ...and this is NOT what a path-only sort would have produced
        path_only = sorted(rows, key=lambda r: r["path"])
        self.assertNotEqual([r["ref"] for r in path_only], [r["ref"] for r in rows],
                             "path-only order equals the returned order -- the tenant "
                             "key is not actually doing anything here")

        # the default (scoped) view is the tenant of record and nothing else
        _, scoped = _get("/api/org/tree")
        self.assertTrue(scoped)
        self.assertTrue(all(r["tenant_id"] == "T-local" for r in scoped))

    def test_30_simulate_now_ms_moves_staleness_in_both_directions(self):
        """now_ms is the ONE clock both halves of the screen band against. Pin
        it from both ends: a clock far in the past must age nothing, a clock far
        in the future must age everything, and the two answers must differ."""
        day = 86400000
        far_past = 1                      # before every seeded placement
        far_future = int(time.time() * 1000) + 3650 * day

        s_past, past = _post("/api/org/simulate", {"ops": [], "now_ms": far_past})
        s_fut, fut = _post("/api/org/simulate", {"ops": [], "now_ms": far_future})
        self.assertEqual(s_past, 200)
        self.assertEqual(s_fut, 200)

        def stale(sim):
            return [f for f in sim["findings"] if f["code"] in ("ORG-009", "ORG-020")]

        self.assertEqual(stale(past), [],
                          "a clock before every placement cannot call anything stale: %r"
                          % stale(past))
        self.assertTrue(stale(fut),
                         "a clock 10 years out must age the seeded charters past the band")
        self.assertNotEqual(len(stale(past)), len(stale(fut)),
                             "now_ms was accepted but had no effect on the answer")

        # everything that is NOT clock-dependent must be identical between the
        # two runs -- now_ms must not perturb structural findings
        def structural(sim):
            return sorted((f["code"], f["subject"]) for f in sim["findings"]
                          if f["code"] not in ("ORG-009", "ORG-020"))
        self.assertEqual(structural(past), structural(fut),
                          "now_ms changed a structural finding -- it must only band staleness")

        # the parameter is genuinely optional (a curl / this suite may omit it)
        s_none, none = _post("/api/org/simulate", {"ops": []})
        self.assertEqual(s_none, 200)
        self.assertEqual(structural(none), structural(fut))

    def test_31_org010_fires_only_when_the_captured_base_is_actually_stale(self):
        """ORG-010 is "the registry moved since this draft captured its base".
        It is the only finding that comes from the CLIENT's payload rather than
        the registry, so it needs both directions pinned: a stale base MUST
        block, and the true base must NOT -- a false ORG-010 makes Rebase the
        only reachable action on a draft that is perfectly current."""
        _, h = _get("/api/org/history")
        true_lines = h["meta"]["domain_index_lines"]
        self.assertGreater(true_lines, 0)

        def codes(body):
            status, sim = _post("/api/org/simulate", body)
            self.assertEqual(status, 200)
            return {f["code"] for f in sim["findings"]}

        # (a) deliberately stale base -> ORG-010, severity block
        status, stale_sim = _post("/api/org/simulate",
                                   {"ops": [], "base": {"domain_index_lines": true_lines + 7}})
        self.assertEqual(status, 200)
        org010 = [f for f in stale_sim["findings"] if f["code"] == "ORG-010"]
        self.assertTrue(org010, "a base that no longer matches the registry must fire ORG-010")
        self.assertEqual(org010[0]["sev"], "block")

        # (b) the true value -> no ORG-010
        self.assertNotIn("ORG-010", codes({"ops": [], "base": {"domain_index_lines": true_lines}}))
        # (c) a base that carries no fingerprint at all -> nothing to compare
        self.assertNotIn("ORG-010", codes({"ops": [], "base": {}}))
        # (d) no base at all -> nothing to compare
        self.assertNotIn("ORG-010", codes({"ops": []}))
        # (e) stale in the OTHER direction (fewer lines than reality) still fires
        self.assertIn("ORG-010",
                      codes({"ops": [], "base": {"domain_index_lines": max(0, true_lines - 1)}}))

        # and the staleness check reads the registry live: after a classify
        # writes a placement, the DOMAIN_INDEX line count is unchanged (classify
        # never mints), so the true base must STILL be clean.
        _post("/api/classify", {"text": "terraform runbook backup restore infrastructure"})
        _, h2 = _get("/api/org/history")
        self.assertEqual(h2["meta"]["domain_index_lines"], true_lines,
                          "classify must not append to the domain index")
        self.assertNotIn("ORG-010", codes({"ops": [], "base": {"domain_index_lines": true_lines}}))

    # ======================================================== providers ====
    # These endpoints shipped without a single test. Everything below asserts
    # an INVARIANT that is checkable on any machine (installed == which(), a
    # refusal carries the reason) rather than hard-coding this laptop's PATH --
    # a test that says "codex is absent" would start lying the day someone
    # installs codex, which is exactly the kind of stale claim this suite
    # exists to prevent.

    @staticmethod
    def _which(binary):
        import shutil as _sh
        return _sh.which(binary)

    def test_35_providers_installed_is_which_and_nothing_else(self):
        """`installed` is the ONE claim the panel renders as a green dot, and
        the failure mode this endpoint was written to prevent is claiming a
        provider is available on the strength of a leftover config directory.
        So: recompute shutil.which() here, independently, and require the
        endpoint to agree -- including for a provider that HAS a config dir but
        no binary, which is the case this machine actually exhibits (~/.codex
        exists, `codex` is not on PATH)."""
        status, body = _get("/api/providers")
        self.assertEqual(status, 200)
        self.assertIn("providers", body)
        ids = [p["id"] for p in body["providers"]]
        self.assertEqual(ids, ["claude", "codex", "gemini"],
                         "the catalogue is fixed and ordered by precedence")

        for p in body["providers"]:
            for field in ("id", "name", "bin", "installed", "configured",
                          "config_dir", "reason", "default", "runnable"):
                self.assertIn(field, p, "%s is missing %r" % (p["id"], field))
            self.assertEqual(
                p["installed"], self._which(p["bin"]) is not None,
                "%s: `installed` must be shutil.which(%r) and nothing else -- "
                "a config directory is not evidence of a binary"
                % (p["id"], p["bin"]))
            self.assertEqual(
                p["configured"], os.path.isdir(os.path.expanduser(p["config_dir"])),
                "%s: `configured` must be the config dir's existence" % p["id"])
            self.assertIn("adapter", p, "%s is missing 'adapter'" % p["id"])
            self.assertEqual(
                p["runnable"],
                p["installed"] and p["configured"] and p["adapter"],
                "%s: runnable == installed AND configured AND adapter -- an "
                "installed CLI we cannot drive is not runnable" % p["id"])
            if p["runnable"]:
                self.assertIsNone(p["reason"], "a runnable provider has no excuse")
            else:
                self.assertTrue(p["reason"],
                                "%s is not runnable and must say why -- the UI has "
                                "nothing else to render" % p["id"])
                # the reason must name the half that failed, with the path, so
                # "unavailable" is never bare
                if p["configured"] and not p["installed"]:
                    self.assertIn(p["bin"], p["reason"])
                    self.assertIn(p["config_dir"], p["reason"])

    def test_36_a_config_dir_alone_never_makes_a_provider_installed(self):
        """The provable negative behind test_35: for every catalogued provider
        whose binary is NOT on PATH, `installed` is False no matter what its
        config directory looks like. On this machine ~/.codex exists and
        `codex` does not -- the exact shape that would tempt an
        availability check into inferring one from the other."""
        _, body = _get("/api/providers")
        absent = [p for p in body["providers"] if self._which(p["bin"]) is None]
        self.assertTrue(absent, "no absent provider to test against on this machine")
        for p in absent:
            self.assertFalse(p["installed"], "%s has no binary" % p["id"])
            self.assertFalse(p["runnable"], "%s cannot run" % p["id"])

    def test_37_active_is_runnable_and_ignored_overrides_are_surfaced(self):
        _, body = _get("/api/providers")
        by_id = {p["id"]: p for p in body["providers"]}
        self.assertIn("active", body)
        self.assertIn("active_source", body)
        self.assertIsInstance(body.get("ignored"), list)
        if body["active"] is not None:
            self.assertTrue(by_id[body["active"]]["runnable"],
                            "the ACTIVE provider must be runnable -- handing back "
                            "an id whose binary is missing moves the failure into "
                            "the websocket, where it looks like a dead socket")
        for ig in body["ignored"]:
            self.assertIn("source", ig)
            self.assertTrue(ig.get("reason"), "a dropped override must say why")

    def test_38_setting_an_unrunnable_provider_is_REFUSED_with_the_reason(self):
        """The point of the whole module: the UI must not be able to select a
        provider that cannot start."""
        _, body = _get("/api/providers")
        before = body["active"]
        blocked = [p for p in body["providers"] if not p["runnable"]]
        self.assertTrue(blocked, "no unrunnable provider to refuse on this machine")
        for p in blocked:
            status, err = _post("/api/providers/active", {"id": p["id"]})
            self.assertEqual(status, 400,
                             "%s is not runnable and must be refused, not stored"
                             % p["id"])
            self.assertIn("not runnable", err["detail"])
            self.assertIn(p["reason"], err["detail"],
                          "the refusal must carry the SPECIFIC reason, not a "
                          "generic 'unavailable'")
        # the refusal is not a partial write: active is untouched
        _, after = _get("/api/providers")
        self.assertEqual(after["active"], before)

    def test_39_unknown_provider_id_is_400_and_lists_the_known_ids(self):
        status, err = _post("/api/providers/active", {"id": "not-a-provider"})
        self.assertEqual(status, 400)
        self.assertIn("unknown provider", err["detail"])
        for known in ("claude", "codex", "gemini"):
            self.assertIn(known, err["detail"])

    def test_40_setting_a_runnable_provider_persists_and_is_readable_back(self):
        _, body = _get("/api/providers")
        runnable = [p for p in body["providers"] if p["runnable"]]
        if not runnable:
            self.skipTest("no runnable provider on this machine")
        pid = runnable[0]["id"]
        status, out = _post("/api/providers/active", {"id": pid})
        self.assertEqual(status, 200)
        self.assertEqual(out["active"], pid)
        _, settings = _get("/api/settings")
        self.assertEqual(settings["settings"]["provider"], pid)
        self.assertEqual(settings["settings"]["provider_source"], "settings",
                         "once chosen, the choice comes from the file -- not "
                         "from the catalog-order fallback")

    # ========================================================= settings ====

    def test_41_settings_shape_and_the_default_is_plan(self):
        status, body = _get("/api/settings")
        self.assertEqual(status, 200)
        st = body["settings"]
        for key in ("provider", "permission_mode", "workdir"):
            self.assertIn(key, st, "%r is part of the contract" % key)
        self.assertEqual(st["permission_mode"], "plan",
                         "SAFETY rule 4: the default must not auto-approve edits")
        modes = {m["id"]: m for m in body["permission_modes"]}
        self.assertEqual(set(modes), {"plan", "acceptEdits", "bypassPermissions"})
        self.assertTrue(modes["plan"]["default"])
        self.assertFalse(modes["plan"]["writes_files"])
        # the two modes that write files must SAY they write files -- this flag
        # is what the panel renders as the warning badge
        self.assertTrue(modes["acceptEdits"]["writes_files"])
        self.assertTrue(modes["bypassPermissions"]["writes_files"])
        for m in body["permission_modes"]:
            self.assertTrue(m["note"], "%s has no explanation" % m["id"])

    def test_42_settings_rejects_an_unknown_permission_mode_without_storing_it(self):
        _, before = _get("/api/settings")
        status, err = _post("/api/settings", {"permission_mode": "yolo"})
        self.assertEqual(status, 400)
        self.assertIn("unknown permission_mode", err["detail"])
        _, after = _get("/api/settings")
        self.assertEqual(after["settings"]["permission_mode"],
                         before["settings"]["permission_mode"],
                         "a rejected mode must not be silently downgraded OR stored")

    def test_43_settings_writes_outside_any_sutra_native_home(self):
        """Panel preferences are not governance state. If the settings file
        landed under SUTRA_NATIVE_HOME, POST /api/settings would be a registry
        write -- which this surface is not allowed to make."""
        _, body = _get("/api/settings")
        path = body["settings"]["settings_path"]
        self.assertFalse(path.startswith(self.tmpdir),
                         "settings must not live inside SUTRA_NATIVE_HOME (%s)" % path)
        self.assertTrue(path.startswith(self.userdir),
                        "the test run must be writing to its own tempdir, not the "
                        "operator's ~/.sutra-ui -- got %s" % path)

    def test_44_empty_settings_post_is_400_not_a_silent_noop(self):
        status, err = _post("/api/settings", {})
        self.assertEqual(status, 400)
        self.assertIn("nothing to update", err["detail"])

    def test_45_settings_partial_update_touches_only_the_key_sent(self):
        _, before = _get("/api/settings")
        target = os.path.join(self.userdir, "wd-roundtrip")
        status, out = _post("/api/settings", {"workdir": target})
        self.assertEqual(status, 200)
        self.assertEqual(out["settings"]["workdir"], target)
        # permission_mode and provider were NOT in the body and must be intact
        self.assertEqual(out["settings"]["permission_mode"],
                         before["settings"]["permission_mode"])
        self.assertEqual(out["settings"]["provider"], before["settings"]["provider"])
        _, reread = _get("/api/settings")
        self.assertEqual(reread["settings"]["workdir"], target)

    def test_46_settings_rejects_an_unrunnable_provider_like_the_other_endpoint(self):
        """POST /api/settings and POST /api/providers/active are two doors to
        the same write. A guard on only one of them is not a guard."""
        _, prov = _get("/api/providers")
        blocked = [p for p in prov["providers"] if not p["runnable"]]
        if not blocked:
            self.skipTest("every catalogued provider is runnable here")
        status, err = _post("/api/settings", {"provider": blocked[0]["id"]})
        self.assertEqual(status, 400)
        self.assertIn("not runnable", err["detail"])

    # ========================================================== tenants ====

    def test_47_tenants_are_derived_from_the_registry_not_padded(self):
        """There is no tenant table -- a tenant is a tenant_id observed on a
        domain or a placement. So the list must be exactly the union of what
        the other endpoints report, plus the tenant of record. Padding it (to
        make a tenant picker look populated) is the failure this guards."""
        status, rows = _get("/api/tenants")
        self.assertEqual(status, 200)
        self.assertIsInstance(rows, list)
        self.assertTrue(rows)

        _, all_domains = _get("/api/org/tree?all_tenants=true&include_retired=true")
        _, all_plc = _get("/api/org/placements?all_tenants=true")
        observed = set(d["tenant_id"] for d in all_domains if d.get("tenant_id"))
        observed |= set(p["tenant_id"] for p in all_plc if p.get("tenant_id"))

        listed = set(r["tenant_id"] for r in rows)
        defaults = [r for r in rows if r["is_default"]]
        self.assertEqual(len(defaults), 1, "exactly one tenant of record")
        self.assertEqual(listed - observed, set(),
                         "every listed tenant must be OBSERVED in the registry -- "
                         "no invented rows")
        self.assertEqual(observed - listed, set(),
                         "every observed tenant must be listed -- none hidden")
        self.assertIn(defaults[0]["tenant_id"], listed)

    def test_48_tenant_counts_match_the_scoped_endpoints_exactly(self):
        _, rows = _get("/api/tenants")
        for r in rows:
            tid = r["tenant_id"]
            _, live = _get("/api/org/tree?tenant=%s" % tid)
            _, everything = _get("/api/org/tree?tenant=%s&include_retired=true" % tid)
            _, chs = _get("/api/org/charters?tenant=%s" % tid)
            _, plc = _get("/api/org/placements?tenant=%s" % tid)
            self.assertEqual(r["domains"], len(everything),
                             "%s: `domains` counts the whole scope" % tid)
            self.assertEqual(r["domains_live"], len(live), tid)
            self.assertEqual(r["domains_retired"],
                             len(everything) - len(live), tid)
            self.assertEqual(r["charters"], len(chs), tid)
            self.assertEqual(r["placements"], len(plc), tid)
            if r["root_ref"]:
                roots = [d for d in everything if d["parent_ref"] is None]
                self.assertIn(r["root_ref"], [d["ref"] for d in roots],
                              "%s: root_ref must be a real parentless domain" % tid)

    # ==================================================== tenant scoping ===

    def test_49_the_tenant_param_actually_scopes_every_org_endpoint(self):
        """?tenant= is the parameter the whole panel hangs off. If it were
        ignored (FastAPI silently drops UNKNOWN query params, so a rename would
        not error), every screen would quietly show all-tenants data while the
        rail said 'T-local'."""
        _, rows = _get("/api/tenants")
        tenants = [r["tenant_id"] for r in rows]
        self.assertGreaterEqual(len(tenants), 2,
                                "the fixture must hold >1 tenant for scoping to be "
                                "provable -- got %r" % tenants)

        seen_refs = {}
        for tid in tenants:
            _, tree = _get("/api/org/tree?tenant=%s&include_retired=true" % tid)
            self.assertTrue(tree, "%s has no domains" % tid)
            for d in tree:
                self.assertEqual(d["tenant_id"], tid,
                                 "?tenant=%s leaked a %s row" % (tid, d["tenant_id"]))
            seen_refs[tid] = set(d["ref"] for d in tree)
            _, plc = _get("/api/org/placements?tenant=%s" % tid)
            for row in plc:
                self.assertEqual(row["tenant_id"], tid,
                                 "?tenant=%s leaked a placement from %s"
                                 % (tid, row["tenant_id"]))

        # the scopes are genuinely disjoint, i.e. the param partitions rather
        # than returning the same set under a different label
        a, b = tenants[0], tenants[1]
        self.assertNotEqual(seen_refs[a], seen_refs[b])
        self.assertEqual(seen_refs[a] & seen_refs[b], set())

        # ...and all_tenants is the union, so nothing is lost either way
        _, everyone = _get("/api/org/tree?all_tenants=true&include_retired=true")
        union = set()
        for tid in tenants:
            union |= seen_refs[tid]
        self.assertEqual(set(d["ref"] for d in everyone), union)

    def test_50_an_unknown_tenant_returns_empty_not_everything(self):
        """The dangerous failure is a typo'd/stale tenant id falling back to
        'unscoped', which silently shows another tenant's org chart."""
        ghost = "T-does-not-exist-%d" % int(time.time())
        for path in ("/api/org/tree?tenant=%s&include_retired=true",
                     "/api/org/charters?tenant=%s",
                     "/api/org/placements?tenant=%s"):
            status, rows = _get(path % ghost)
            self.assertEqual(status, 200)
            self.assertEqual(rows, [],
                             "%s must be EMPTY for an unknown tenant, never a "
                             "fallback to the full registry" % (path % ghost))

    def test_51_stats_scope_follows_the_tenant_param(self):
        _, rows = _get("/api/tenants")
        for r in rows:
            _, st = _get("/api/org/stats?tenant=%s" % r["tenant_id"])
            self.assertEqual(st["tenant_id"], r["tenant_id"])
            self.assertEqual(st["domains"], r["domains"])
            self.assertEqual(st["charters"], r["charters"])
            self.assertEqual(st["placements"], r["placements"])

    # ================================================ sessions: NO FIXTURES =

    def test_52_sessions_are_real_files_under_dot_claude_projects(self):
        """The panel used to manufacture sessions by grouping placement rows by
        path segment ("Scratchpad -- 3 turns"). Nothing like that ever existed.
        Every id this endpoint returns must be a file that is actually on
        disk -- that is the only definition of "real" available here."""
        import glob
        status, rows = _get("/api/sessions")
        self.assertEqual(status, 200)
        self.assertIsInstance(rows, list)
        projects = os.path.expanduser("~/.claude/projects")
        if not os.path.isdir(projects):
            self.skipTest("no ~/.claude/projects on this machine")
        for r in rows:
            hits = glob.glob(os.path.join(projects, "*", r["id"] + ".jsonl"))
            self.assertTrue(hits,
                            "session %r is not a transcript on disk -- it was "
                            "invented" % r["id"])
            self.assertEqual(r["size"], os.path.getsize(hits[0]),
                             "%s: size must be the file's, not a guess" % r["id"])
            self.assertEqual(r["project"], os.path.basename(os.path.dirname(hits[0])))
            # The title must be a QUOTATION from the transcript, never
            # generated. Re-derive the candidate prompts here from the raw
            # JSONL -- independently of session_reader -- and require the
            # title to be the head of one of them.
            if r["title"] == "(no prompt)":
                continue
            prompts = []
            with io.open(hits[0], encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i > 400:
                        break
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("type") != "user":
                        continue
                    msg = ev.get("message")
                    if not isinstance(msg, dict):
                        continue
                    c = msg.get("content")
                    if isinstance(c, str):
                        prompts.append(c)
                    elif isinstance(c, list):
                        prompts.append("\n".join(
                            b.get("text", "") for b in c
                            if isinstance(b, dict) and b.get("type") == "text"))
            norm = lambda t: " ".join(t.split())
            title = norm(r["title"])
            self.assertTrue(
                any(norm(t).startswith(title[:40]) for t in prompts if t),
                "%s: title %r is not the head of any user prompt in the "
                "transcript -- it was manufactured" % (r["id"], r["title"]))

    def test_53_the_shipped_panel_contains_no_session_fabricator(self):
        """A provable negative for the deleted seedSessions(): the string may
        appear in the tombstone comment explaining WHY it is gone, but never as
        a definition or a call."""
        import re
        html = io.open(os.path.join(HERE, "static", "panel.html"),
                       encoding="utf-8").read()
        # strip /* ... */ comments -- the tombstone lives in one
        code = re.sub(r"/\*.*?\*/", "", html, flags=re.S)
        code = re.sub(r"(?m)^\s*//.*$", "", code)
        for banned in (r"function\s+seedSessions", r"seedSessions\s*\(",
                       r"seeded\s*:\s*true"):
            self.assertIsNone(re.search(banned, code),
                              "panel.html still contains %r outside a comment -- "
                              "the session fabricator is back" % banned)

    def test_32_workdir_exists_after_importing_app(self):
        """Both socket handlers spawn a subprocess with cwd=WORKDIR. If that
        directory does not exist, create_subprocess_exec raises FileNotFoundError
        BEFORE a single frame is written: the socket dies, and the operator sees
        a UI that does nothing -- no error text, no output. WORKDIR defaults to
        ~/sutra-ui-workspace, which nothing else on the system creates, so a
        fresh machine was the guaranteed-broken case. Importing the app must
        leave the directory usable."""
        probe = tempfile.mkdtemp(prefix="sutra-test-workdir-")
        try:
            target = os.path.join(probe, "does", "not", "exist", "yet")
            self.assertFalse(os.path.isdir(target))
            env = dict(os.environ)
            env["SUTRA_UI_WORKDIR"] = target
            env["SUTRA_NATIVE_HOME"] = self.tmpdir
            out = subprocess.check_output(
                [VENV_PY, "-c",
                 "import os, app; print(app.WORKDIR); print(os.path.isdir(app.WORKDIR))"],
                cwd=HERE, env=env, stderr=subprocess.DEVNULL,
            ).decode("utf-8").strip().splitlines()
            self.assertEqual(out[-2], target, "WORKDIR must honour SUTRA_UI_WORKDIR")
            self.assertEqual(out[-1], "True",
                              "importing app must leave WORKDIR on disk -- otherwise the "
                              "first /ws/chat message dies with FileNotFoundError")
            self.assertTrue(os.path.isdir(target),
                             "the directory must really be there, not merely reported")
        finally:
            shutil.rmtree(probe, ignore_errors=True)

        # the guard itself must survive a workdir it cannot create (a file in
        # the way) by returning None rather than raising into the handshake
        import app as app_mod
        blocker = tempfile.mkdtemp(prefix="sutra-test-blocker-")
        try:
            f = os.path.join(blocker, "not-a-dir")
            with open(f, "w") as fh:
                fh.write("x")
            self.assertIsNone(app_mod._ensure_workdir(f))
        finally:
            shutil.rmtree(blocker, ignore_errors=True)


class TestChatRefusesApiKey(unittest.TestCase):
    """/ws/chat with ANTHROPIC_API_KEY set in the SERVER environment.

    A key in the server env makes the spawned `claude` bill the Anthropic API
    instead of the operator's Max subscription. ws_term already refused; ws_chat
    must refuse identically, and it must refuse BEFORE spawning anything -- so
    this test never runs a real `claude`, never bills anything, and asserts on
    the refusal frame itself. CLAUDE_BIN is additionally pointed at a path that
    cannot exist, so a regression that spawns anyway fails loudly rather than
    quietly costing money.
    """

    proc = None
    port = None
    tmpdir = None

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="sutra-test-wschat-")
        sys.path.insert(0, os.path.join(HERE, "..", "lib"))
        sys.path.insert(0, HERE)
        import fixture_seed  # noqa: E402
        fixture_seed.seed(cls.tmpdir)

        cls.port = _free_port()
        env = dict(os.environ)
        env["SUTRA_NATIVE_HOME"] = cls.tmpdir
        env["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"
        # belt and braces: if the refusal ever regresses, the spawn fails
        # instead of running a real, billable claude turn.
        env["SUTRA_UI_CLAUDE_BIN"] = os.path.join(cls.tmpdir, "no-such-claude-binary")
        env["SUTRA_UI_WORKDIR"] = os.path.join(cls.tmpdir, "workspace")
        cls.proc = subprocess.Popen(
            [VENV_PY, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
             "--port", str(cls.port), "--log-level", "warning"],
            cwd=HERE, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/api/org/stats" % cls.port, timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        else:
            raise RuntimeError("ws-chat server did not come up")

    @classmethod
    def tearDownClass(cls):
        if cls.proc:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
                cls.proc.wait(timeout=5)
        if cls.tmpdir and os.path.isdir(cls.tmpdir):
            shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_33_ws_chat_refuses_when_an_api_key_is_in_the_server_env(self):
        from websockets.sync.client import connect

        url = "ws://127.0.0.1:%d/ws/chat" % self.port
        with connect(url, open_timeout=10) as ws:
            frame = json.loads(ws.recv(timeout=10))
            self.assertEqual(frame["type"], "error",
                              "an API key in the server env must produce a refusal frame")
            detail = frame["detail"]
            self.assertIn("ANTHROPIC_API_KEY", detail)
            self.assertIn("Refused", detail)
            # the refusal must be terminal: the socket closes, and no run frames
            # (start/token/done) are ever emitted -- i.e. nothing was billed.
            with self.assertRaises(Exception):
                while True:
                    nxt = json.loads(ws.recv(timeout=5))
                    self.fail("expected the socket to close after refusing, got %r" % nxt)

        # a second connection behaves identically -- the refusal is not a
        # one-shot flag that a reconnect walks around
        with connect(url, open_timeout=10) as ws2:
            again = json.loads(ws2.recv(timeout=10))
            self.assertEqual(again["type"], "error")
            self.assertIn("ANTHROPIC_API_KEY", again["detail"])

        # the server is still healthy: refusing a socket must not kill the app
        req = urllib.request.Request("http://127.0.0.1:%d/api/org/stats" % self.port)
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)

    def test_34_ws_term_refuses_on_the_same_condition(self):
        """The two sockets must agree: a key that is unsafe for /ws/chat is
        unsafe for /ws/term. ws_term answers in raw terminal text, not JSON."""
        from websockets.sync.client import connect

        with connect("ws://127.0.0.1:%d/ws/term" % self.port, open_timeout=10) as ws:
            msg = ws.recv(timeout=10)
            if isinstance(msg, bytes):
                msg = msg.decode("utf-8", "replace")
            self.assertIn("Refused", msg)
            self.assertIn("ANTHROPIC_API_KEY", msg)


class TestEffectiveModeAndOnboarding(unittest.TestCase):
    """The stored permission mode is not always the one that runs, and the panel
    used to report only the stored one -- so it rendered "nothing will prompt you
    per edit" while ws_chat was in fact spawning `plan`. These tests pin the two
    values apart, and pin the first-run flag that gates the onboarding.

    providers.py is exercised directly (not over HTTP) so SUTRA_UI_SETTINGS can
    point at a tempdir: the operator's real ~/.sutra-ui/settings.json must never
    be read or written by the suite.
    """

    def setUp(self):
        import importlib
        self.tmp = tempfile.mkdtemp(prefix="sutra-ui-settings-")
        self.path = os.path.join(self.tmp, "settings.json")
        self._old = dict(os.environ)
        os.environ["SUTRA_UI_SETTINGS"] = self.path
        os.environ.pop("SUTRA_UI_ALLOW_UNSAFE_PERM_MODES", None)
        os.environ.pop("SUTRA_UI_PERMISSION_MODE", None)
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import providers
        self.providers = importlib.reload(providers)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, obj):
        with io.open(self.path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    def test_unsafe_stored_mode_is_reported_as_clamped(self):
        """A bypassPermissions on file without the opt-in must be reported as
        NOT running -- this is the exact state that made the panel lie."""
        self._write({"permission_mode": "bypassPermissions"})
        s = self.providers.load_settings()
        self.assertEqual(s["permission_mode"], "bypassPermissions")
        self.assertEqual(s["permission_mode_effective"], "plan")
        self.assertTrue(s["permission_mode_clamped"])
        self.assertIn("SUTRA_UI_ALLOW_UNSAFE_PERM_MODES",
                      s["permission_mode_clamp_reason"])

    def test_safe_mode_is_never_marked_clamped(self):
        self._write({"permission_mode": "plan"})
        s = self.providers.load_settings()
        self.assertEqual(s["permission_mode_effective"], "plan")
        self.assertFalse(s["permission_mode_clamped"])
        self.assertIsNone(s["permission_mode_clamp_reason"])

    def test_opt_in_honours_the_stored_unsafe_mode(self):
        """With the out-of-band env set, stored and effective must agree --
        otherwise the escape hatch documented in the UI does not exist."""
        import importlib
        self._write({"permission_mode": "bypassPermissions"})
        os.environ["SUTRA_UI_ALLOW_UNSAFE_PERM_MODES"] = "1"
        prov = importlib.reload(self.providers)
        s = prov.load_settings()
        self.assertEqual(s["permission_mode_effective"], "bypassPermissions")
        self.assertFalse(s["permission_mode_clamped"])
        self.assertTrue(s["unsafe_modes_allowed"])

    def test_onboarded_defaults_false_and_round_trips(self):
        """First run is decided by the settings FILE, not a browser flag, so the
        disclosure cannot be skipped by clearing localStorage."""
        self.assertFalse(self.providers.load_settings()["onboarded"])
        self.providers.save_settings(onboarded=True)
        self.assertTrue(self.providers.load_settings()["onboarded"])

    def test_onboarded_rejects_non_boolean(self):
        with self.assertRaises(ValueError):
            self.providers.save_settings(onboarded="yes")

    def test_onboarding_write_preserves_other_settings(self):
        """Dismissing onboarding must not reset the operator's provider/workdir."""
        self._write({"permission_mode": "plan", "provider": "claude"})
        self.providers.save_settings(onboarded=True)
        with io.open(self.path, encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertEqual(raw["provider"], "claude")
        self.assertEqual(raw["permission_mode"], "plan")
        self.assertTrue(raw["onboarded"])


class TestLoginPathRepair(unittest.TestCase):
    """A Finder-launched .app inherits launchd's PATH, so /opt/homebrew/bin is
    absent and EVERY provider reported "binary not on PATH" on a machine where
    the binary runs fine in any terminal. Chat was dead and the stated reason
    pointed at the wrong thing.

    These pin both halves: it repairs a GUI-style PATH, and it stays out of the
    way when PATH is already fine (a login-shell spawn on every CLI start would
    be pure latency plus running the operator's rc files for nothing).
    """

    def setUp(self):
        import importlib
        self._old = dict(os.environ)
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import providers
        self.providers = importlib.reload(providers)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old)

    def test_no_op_when_path_already_resolves_a_provider(self):
        """Must not spawn a login shell when it has nothing to fix."""
        import importlib
        real = shutil.which("claude") or shutil.which("codex")
        if not real:
            self.skipTest("no catalogued CLI on PATH here; nothing to no-op against")
        prov = importlib.reload(self.providers)
        called = {"n": 0}
        prov._login_shell_path = lambda: (called.__setitem__("n", called["n"] + 1), None)[1]
        self.assertFalse(prov.ensure_login_path())
        self.assertEqual(called["n"], 0, "spawned a login shell despite PATH being usable")

    def test_repairs_a_launchd_style_path(self):
        import importlib
        real = shutil.which("claude") or shutil.which("codex")
        if not real:
            self.skipTest("no catalogued CLI on PATH here to rediscover")
        os.environ["PATH"] = "/usr/bin:/bin"
        prov = importlib.reload(self.providers)
        self.assertIsNone(shutil.which("claude"), "precondition: claude must be hidden")
        prov._login_shell_path = lambda: os.path.dirname(real) + os.pathsep + "/usr/bin"
        self.assertTrue(prov.ensure_login_path())
        self.assertIn(os.path.dirname(real), os.environ["PATH"])

    def test_runs_at_most_once(self):
        import importlib
        os.environ["PATH"] = "/usr/bin:/bin"
        prov = importlib.reload(self.providers)
        prov._login_shell_path = lambda: "/some/injected/dir"
        self.assertTrue(prov.ensure_login_path())
        self.assertFalse(prov.ensure_login_path(), "repaired PATH twice")

    def test_ignores_rc_banner_noise_and_non_path_output(self):
        """An rc file that prints a banner must not poison PATH."""
        import importlib
        os.environ["PATH"] = "/usr/bin:/bin"
        prov = importlib.reload(self.providers)
        prov._login_shell_path = lambda: None      # what the parser returns for junk
        self.assertFalse(prov.ensure_login_path())
        self.assertEqual(os.environ["PATH"], "/usr/bin:/bin")

    def test_appends_so_an_explicit_path_still_wins(self):
        """A PATH set deliberately for this process must keep precedence."""
        import importlib
        os.environ["PATH"] = "/usr/bin:/bin"
        prov = importlib.reload(self.providers)
        prov._login_shell_path = lambda: "/late/dir"
        prov.ensure_login_path()
        self.assertTrue(os.environ["PATH"].startswith("/usr/bin:/bin"),
                        "login PATH was prepended; it must be appended")


if __name__ == "__main__":
    unittest.main()
