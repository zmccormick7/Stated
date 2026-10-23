import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from premisedb import RecordStore
from premisedb import feature_runs
from premisedb.feature_runs import run_features
from premisedb.ui.harness import HarnessError, Reply, UsageLimit


class FakeHarness:
    """Stands in for an agent: at the session's cutoff it writes a value, confirms the latest, or does nothing,
    as the script for that (subject, date) says."""

    name = "fake"

    def __init__(self, db_path, feature, script):
        self.db_path, self.feature, self.script, self.calls = db_path, feature, script, []

    def run(self, prompt, *, cwd, env, web, writable, timeout):
        subject = prompt.split("(Entity ")[1].split(")")[0]
        day = env["PREMISEDB_AS_OF"]
        self.calls.append((subject, day, web, env["PREMISEDB_FETCH"], Path(cwd).is_dir(), tuple(writable), prompt))
        action = self.script.get((subject, day), "value")
        if isinstance(action, Exception):
            raise action
        with RecordStore(self.db_path, as_of=day) as db:
            if action == "value":
                made = db.contribute(author_id="agent", records=[{"ref": "v", "kind": "Variable", "text": "reading",
                    "attributes": {"key": "runnable", "feature": self.feature.id, "value": 0.5}}],
                    links=[{"from_id": "v", "to_id": subject, "relation": "about"}]).records["v"]
                return Reply(f"{made.id} 0.5", None, ())
            if action == "confirm":
                from premisedb.features import _latest
                latest = _latest(db, self.feature.id, subject)
                return Reply(db.confirm_value(latest.id, author_id="agent").value_id, None, ())
        return Reply("I could not decide.", None, ())


class FeatureRunTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        with RecordStore(self.path) as db:
            self.a = db.create("Entity", text="Bank A", author_id="setup").id
            self.b = db.create("Entity", text="Bank B", author_id="setup").id
            self.feature = db.define_feature(author_id="r", key="runnable", type="number", min=0, max=1,
                                             instructions="Share of deposits that could leave within weeks.")
            db.schedule_feature("runnable", every="week", start="2023-01-02")
            db.define_feature(author_id="r", key="runnable_pct", type="number", formula="runnable * 100",
                              instructions="The runnable share in percent.")

    def run_with(self, script, **options):
        harness = FakeHarness(self.path, self.feature, script)
        seen = []
        result = run_features(self.path, "runnable", [self.a, self.b], harness=harness, end="2023-01-16",
                              on_result=seen.append, **options)
        return harness, result, seen

    def test_each_subject_runs_its_dates_in_order_at_their_cutoffs(self):
        harness, result, seen = self.run_with({(self.a, "2023-01-09"): "confirm"}, workers=2)
        self.assertEqual(sorted((s, d) for s, d, *_ in harness.calls),
                         sorted((s, d) for s in (self.a, self.b) for d in ("2023-01-02", "2023-01-09", "2023-01-16")))
        for subject in (self.a, self.b):
            self.assertEqual([d for s, d, *_ in harness.calls if s == subject], ["2023-01-02", "2023-01-09", "2023-01-16"])
        subject, day, web, fetch, folder, writable, prompt = harness.calls[0]
        self.assertEqual((web, fetch, folder, writable), (False, "0", True, (self.path.parent.resolve(),)))
        self.assertIn("premisedb-state", prompt)
        self.assertIn('author_id "feature-run/fake"', prompt)
        self.assertEqual((result.remaining, len(result.sessions), all(s.done for s in result.sessions)), (0, 6, True))
        self.assertEqual(len(seen), 6 + 2)  # Each subject's derived feature, computed once: it never changes.
        self.assertEqual({(d.subject_id, d.date, d.key, d.value) for d in result.derived},
                         {(s, "2023-01-02", "runnable_pct", 50.0) for s in (self.a, self.b)})
        self.assertEqual(self.run_with({})[1].sessions, ())  # Nothing left: running again does nothing.

    def test_a_failed_date_stops_its_subject_and_the_run_resumes_from_it(self):
        harness, result, _ = self.run_with({(self.a, "2023-01-09"): "nothing",
                                            (self.b, "2023-01-02"): HarnessError("crashed")})
        self.assertEqual([(s, d) for s, d, *_ in harness.calls], [(self.a, "2023-01-02"), (self.a, "2023-01-09"),
                                                                  (self.b, "2023-01-02")])
        failed = [s for s in result.sessions if not s.done]
        self.assertEqual([(s.subject, s.date) for s in failed], [("Bank A", "2023-01-09"), ("Bank B", "2023-01-02")])
        self.assertIn("without a value or a check", failed[0].detail)
        self.assertIn("crashed", failed[1].detail)
        self.assertEqual(result.remaining, 5)
        harness, result, _ = self.run_with({}, max_sessions=2)
        self.assertEqual([(s, d) for s, d, *_ in harness.calls], [(self.a, "2023-01-09"), (self.a, "2023-01-16")])
        self.assertEqual(result.remaining, 3)

    def test_a_usage_limit_is_waited_out_and_the_session_tried_again(self):
        script = {(self.a, "2023-01-02"): UsageLimit("limit reached", None)}
        harness = FakeHarness(self.path, self.feature, script)
        original = harness.run

        def once(*args, **kwargs):
            if len(harness.calls) == 1 and script:
                script.clear()
            return original(*args, **kwargs)

        harness.run = once
        with mock.patch.object(feature_runs, "_sleep") as sleep:
            result = run_features(self.path, "runnable", [self.a], harness=harness, end="2023-01-02")
        sleep.assert_called_once_with(feature_runs.RETRY_MINUTES * 60)
        self.assertEqual((len(harness.calls), result.sessions[0].done), (2, True))

    def test_runs_take_workers_and_limits_in_range(self):
        for options in ({"workers": 0}, {"workers": 33}, {"max_sessions": 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.run_with({}, **options)
        import contextlib, io
        from premisedb.cli import main
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(main(["--db", str(self.path), "--as-of", "2023-01-02", "run-features", "runnable",
                                   "--subject", self.a]), 1)
        self.assertIn("runs without a cutoff", json.loads(errors.getvalue())["error"])


if __name__ == "__main__":
    unittest.main()
