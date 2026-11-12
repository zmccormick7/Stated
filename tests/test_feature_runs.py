import contextlib
import io
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from stated import RecordStore
from stated import feature_runs
from stated.cli import main
from stated.features import _latest
from stated.feature_runs import run_features
from stated.harness import HarnessError, UsageLimit


class FakeHarness:
    """Stands in for an agent: at the session's cutoff it writes a value of each feature listed, confirms the
    latest, or does nothing, as the script for that (subject, date) says."""

    name = "fake"

    def __init__(self, db_path, script):
        self.db_path, self.script, self.calls = db_path, script, []

    def run(self, prompt, *, cwd, env, writable, timeout):
        subject = prompt.split("(Entity ")[1].split(")")[0]
        features = re.findall(r"^- (\w+) \(definition ([\w-]+)\)$", prompt, re.M)
        day = env["STATED_AS_OF"]
        self.calls.append((subject, day, tuple(key for key, _ in features), env["STATED_FETCH"],
                           Path(cwd).is_dir(), tuple(writable), prompt))
        action = self.script.get((subject, day), "value")
        if isinstance(action, Exception):
            raise action
        with RecordStore(self.db_path, as_of=day) as db:
            for key, feature_id in features:
                if action == "value":
                    db.contribute(author_id="agent", records=[{"ref": "v", "kind": "Variable", "text": "reading",
                        "attributes": {"key": key, "feature": feature_id, "value": 0.5}}],
                        links=[{"from_id": "v", "to_id": subject, "relation": "about"}])
                elif action == "confirm":
                    db.confirm_value(_latest(db, feature_id, subject).id, author_id="agent")
        return "done" if action in ("value", "confirm") else "I could not decide."


class FeatureRunTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        with RecordStore(self.path) as db:
            self.a = db.create("Entity", text="Bank A", author_id="setup").id
            self.b = db.create("Entity", text="Bank B", author_id="setup").id
            for key in ("runnable", "stable"):
                db.define_feature(author_id="r", key=key, type="number", min=0, max=1, instructions=f"The {key} share.")
            db.schedule_feature("runnable", every="week", start="2023-01-02")
            db.schedule_feature("stable", every="month", start="2023-01-02")
            db.define_feature(author_id="r", key="runnable_pct", type="number", formula="runnable * 100",
                              instructions="The runnable share in percent.")

    def run_with(self, script, features=("runnable",), **options):
        harness = FakeHarness(self.path, script)
        seen = []
        result = run_features(self.path, list(features), [self.a, self.b], harness=harness, end="2023-01-16",
                              on_result=seen.append, **{"skip_unchanged": False, **options})
        return harness, result, seen

    def source(self, subject, published):
        with RecordStore(self.path) as db:
            made = db.create("Source", text=f"Filing of {published}", author_id="setup",
                             attributes={"published": published})
            db.create_link(made.id, subject, "about", author_id="setup")

    def test_each_subject_runs_its_dates_in_order_at_their_cutoffs(self):
        harness, result, seen = self.run_with({(self.a, "2023-01-09"): "confirm"}, workers=2)
        for subject in (self.a, self.b):
            self.assertEqual([d for s, d, *_ in harness.calls if s == subject], ["2023-01-02", "2023-01-09", "2023-01-16"])
        subject, day, keys, fetch, folder, writable, prompt = harness.calls[0]
        self.assertEqual((keys, fetch, folder, writable), (("runnable",), "0", True, (self.path.parent.resolve(),)))
        self.assertIn("stated-state", prompt)
        self.assertIn('author_id "feature-run/fake"', prompt)
        self.assertEqual((result.remaining, len(result.sessions), all(s.done and s.session for s in result.sessions)),
                         (0, 6, True))
        self.assertEqual(len(seen), 6 + 2)  # Each subject's derived feature, computed once: it never changes.
        self.assertEqual({(d.subject_id, d.date, d.key, d.value) for d in result.derived},
                         {(s, "2023-01-02", "runnable_pct", 50.0) for s in (self.a, self.b)})
        self.assertEqual(self.run_with({})[1].sessions, ())  # Nothing left: running again does nothing.

    def test_features_due_on_a_date_share_one_session(self):
        harness, result, _ = self.run_with({}, features=("runnable", "stable"))
        self.assertEqual([(s, d, keys) for s, d, keys, *_ in harness.calls if s == self.a],
                         [(self.a, "2023-01-02", ("runnable", "stable")), (self.a, "2023-01-09", ("runnable",)),
                          (self.a, "2023-01-16", ("runnable",))])
        self.assertEqual((result.keys, result.remaining, len(harness.calls)), (("runnable", "stable"), 0, 6))

    def test_dates_without_a_new_document_are_confirmed_without_a_session(self):
        self.source(self.a, "2022-12-30")
        self.source(self.a, "2023-01-12")  # Read by the session on 2023-01-16, not before.
        self.source(self.b, "2023-01-03")
        harness, result, _ = self.run_with({}, skip_unchanged=True)
        self.assertEqual(sorted((s, d) for s, d, *_ in harness.calls),
                         sorted([(self.a, "2023-01-02"), (self.a, "2023-01-16"), (self.b, "2023-01-02"),
                                 (self.b, "2023-01-09")]))
        quiet = [(s.subject, s.date) for s in result.sessions if not s.session]
        self.assertEqual(sorted(quiet), [("Bank A", "2023-01-09"), ("Bank B", "2023-01-16")])
        self.assertEqual(result.remaining, 0)
        with RecordStore(self.path) as db:
            check = db._db.execute("SELECT author_id FROM feature_checks").fetchall()
            self.assertEqual({row[0] for row in check}, {"feature-run/fake/no-new-documents"})

    def test_a_session_is_told_which_documents_are_new(self):
        self.source(self.a, "2022-12-30")
        self.source(self.a, "2023-01-12")
        harness, _, _ = self.run_with({}, skip_unchanged=True)
        prompts = {(s, d): prompt for s, d, *_, prompt in harness.calls}
        first, later = prompts[(self.a, "2023-01-02")], prompts[(self.a, "2023-01-16")]
        self.assertIn("has no value yet", first)  # No reading to count from, so the latest documents are listed.
        self.assertIn("- 2022-12-30: Filing of 2022-12-30 (Source ", first)
        self.assertNotIn("2023-01-12", first)  # Not yet published at this cutoff.
        self.assertIn("published since these features were last read (2023-01-09)", later)
        self.assertIn("- 2023-01-12: Filing of 2023-01-12 (Source ", later)
        self.assertNotIn("Filing of 2022-12-30", later)  # Read before; only what is new is listed.
        self.assertIn("Read each of them before you write or confirm a value.", later)
        self.assertIn("No document about the subject is visible.", prompts[(self.b, "2023-01-02")])

    def test_a_session_with_no_new_document_is_told_so(self):
        self.source(self.a, "2022-12-30")
        harness, _, _ = self.run_with({})  # Every date gets a session, new document or not.
        prompt = next(p for s, d, *_, p in harness.calls if (s, d) == (self.a, "2023-01-09"))
        self.assertIn("No document about the subject has been published since these features were last read "
                      "(2023-01-02).", prompt)

    def test_sources_about_a_record_come_newest_first_as_the_cutoff_sees_them(self):
        for published in ("2023-01-05", "2022-12-30", "2023-01-12"):
            self.source(self.a, published)
        self.source(self.b, "2023-01-06")
        with RecordStore(self.path) as db:
            undated = db.create("Source", text="Undated note", author_id="setup")
            db.create_link(undated.id, self.a, "about", author_id="setup")
            dated = lambda page: [r.attributes.get("published") for r in page.records]
            self.assertEqual(dated(db.sources_about(self.a)), ["2023-01-12", "2023-01-05", "2022-12-30", None])
            self.assertEqual(dated(db.sources_about(self.a, since="2023-01-05")), ["2023-01-12"])
            page = db.sources_about(self.a, limit=2)
            self.assertEqual((dated(page), page.has_more), (["2023-01-12", "2023-01-05"], True))
            with self.assertRaisesRegex(ValueError, "since must be a date"):
                db.sources_about(self.a, since="January")
        with RecordStore(self.path, as_of="2023-01-06") as db:
            self.assertEqual(dated(db.sources_about(self.a)), ["2023-01-05", "2022-12-30"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["--db", str(self.path), "sources", self.a, "--since", "2023-01-01", "--limit", "1"])
        listed = json.loads(out.getvalue())
        self.assertEqual(([r["published"] for r in listed["records"]], listed["has_more"]), (["2023-01-12"], True))

    def test_an_expired_reading_gets_a_session_without_a_new_document(self):
        self.source(self.a, "2022-12-30")
        with RecordStore(self.path, as_of="2023-01-02") as db:
            db.contribute(author_id="agent", records=[{"ref": "v", "kind": "Variable", "text": "reading",
                "attributes": {"key": "runnable", "feature": db.read_feature("runnable").id, "value": 0.5,
                               "expires": "2023-01-16"}}], links=[{"from_id": "v", "to_id": self.a, "relation": "about"}])
        # The check made without a session carries the expiry forward; the session confirms with none.
        harness = FakeHarness(self.path, {(self.a, "2023-01-16"): "confirm"})
        result = run_features(self.path, ["runnable"], [self.a], harness=harness, end="2023-01-23")
        self.assertEqual([(s, d) for s, d, *_ in harness.calls], [(self.a, "2023-01-16")])
        self.assertEqual([(s.date, s.session) for s in result.sessions],
                         [("2023-01-09", False), ("2023-01-16", True), ("2023-01-23", False)])
        with RecordStore(self.path) as db:
            self.assertEqual([tuple(row) for row in db._db.execute("SELECT as_of, expires FROM feature_checks ORDER BY as_of")],
                             [("2023-01-09", "2023-01-16"), ("2023-01-16", None), ("2023-01-23", None)])

    def test_a_failed_date_stops_its_subject_and_the_run_resumes_from_it(self):
        harness, result, _ = self.run_with({(self.a, "2023-01-09"): "nothing",
                                            (self.b, "2023-01-02"): HarnessError("crashed")})
        self.assertEqual([(s, d) for s, d, *_ in harness.calls], [(self.a, "2023-01-02"), (self.a, "2023-01-09"),
                                                                  (self.b, "2023-01-02")])
        failed = [s for s in result.sessions if not s.done]
        self.assertEqual([(s.subject, s.date) for s in failed], [("Bank A", "2023-01-09"), ("Bank B", "2023-01-02")])
        self.assertIn("without a value or a check for runnable", failed[0].detail)
        self.assertIn("crashed", failed[1].detail)
        self.assertEqual(result.remaining, 5)
        harness, result, _ = self.run_with({}, max_sessions=2)
        self.assertEqual([(s, d) for s, d, *_ in harness.calls], [(self.a, "2023-01-09"), (self.a, "2023-01-16")])
        self.assertEqual(result.remaining, 3)

    def test_a_usage_limit_is_waited_out_and_the_session_tried_again(self):
        script = {(self.a, "2023-01-02"): UsageLimit("limit reached", None)}
        harness = FakeHarness(self.path, script)
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
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(main(["--db", str(self.path), "--as-of", "2023-01-02", "run-features", "runnable",
                                   "--subject", self.a]), 1)
        self.assertIn("runs without a cutoff", json.loads(errors.getvalue())["error"])


if __name__ == "__main__":
    unittest.main()
