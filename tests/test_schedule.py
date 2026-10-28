import contextlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stated import RecordStore
from stated.cli import main
from stated.features import scheduled_dates


class ScheduleTests(unittest.TestCase):
    """A feature's schedule says when a value is due for each subject; a date is done once a value carries it
    or a check confirms the latest value still held, and `pending` lists the rest."""

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.db = RecordStore(self.path)
        self.addCleanup(self.db.close)
        self.a = self.db.create("Entity", text="Bank A", author_id="setup")
        self.b = self.db.create("Entity", text="Bank B", author_id="setup")
        self.feature = self.db.define_feature(author_id="r", key="runnable", type="number", min=0, max=1,
                                              instructions="Share of deposits that could leave within weeks.")

    def at(self, day):
        store = RecordStore(self.path, as_of=day)
        self.addCleanup(store.close)
        return store

    def value(self, store, subject, value, updates=None):
        links = [{"from_id": "v", "to_id": subject.id, "relation": "about"}]
        if updates is not None:
            links.append({"from_id": "v", "to_id": updates.id, "relation": "updates"})
        return store.contribute(author_id="agent", records=[{"ref": "v", "kind": "Variable", "text": "reading",
            "attributes": {"key": "runnable", "feature": self.feature.id, "value": value}}], links=links).records["v"]

    def test_scheduled_dates(self):
        self.assertEqual(scheduled_dates("day", "2023-01-30", "2023-01-30", "2023-02-02"),
                         ["2023-01-30", "2023-01-31", "2023-02-01", "2023-02-02"])
        self.assertEqual(scheduled_dates("weekday", "2023-01-05", "2023-01-05", "2023-01-10"),
                         ["2023-01-05", "2023-01-06", "2023-01-09", "2023-01-10"])
        self.assertEqual(scheduled_dates("week", "2023-01-02", "2023-01-10", "2023-01-31"),
                         ["2023-01-16", "2023-01-23", "2023-01-30"])
        self.assertEqual(scheduled_dates("month", "2024-01-31", "2024-01-01", "2024-04-30"),
                         ["2024-01-31", "2024-02-29", "2024-03-31", "2024-04-30"])
        self.assertEqual(scheduled_dates("quarter", "2022-11-30", "2023-01-01", "2023-12-31"),
                         ["2023-02-28", "2023-05-30", "2023-08-30", "2023-11-30"])
        self.assertEqual(scheduled_dates("week", "2023-01-02", "2022-01-01", "2023-01-01"), [])

    def test_a_schedule_is_set_cleared_and_checked(self):
        scheduled = self.db.schedule_feature("runnable", every="week", start="2023-01-02")
        self.assertEqual((scheduled.every, scheduled.start), ("week", "2023-01-02"))
        self.assertEqual(self.db.schedule_feature(self.feature.id, every=None).every, None)
        derived = self.db.define_feature(author_id="r", key="twice", type="number", formula="runnable * 2",
                                         instructions="Twice the runnable share.")
        for args, message in (({"every": "hourly", "start": "2023-01-02"}, "every is one of"),
                              ({"every": "week"}, "start must be a date"),
                              ({"every": None, "start": "2023-01-02"}, "takes no start")):
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, message):
                self.db.schedule_feature("runnable", **args)
        with self.assertRaisesRegex(ValueError, "derived feature has no schedule"):
            self.db.schedule_feature(derived.id, every="week", start="2023-01-02")
        with self.assertRaisesRegex(ValueError, "without a cutoff"):
            self.at("2023-01-09").schedule_feature("runnable", every="week", start="2023-01-02")
        with self.assertRaisesRegex(ValueError, "has no schedule"):
            self.db.pending_work("runnable", [self.a.id])

    def test_new_instructions_keep_the_schedule_they_replace(self):
        self.db.schedule_feature("runnable", every="quarter", start="2023-03-31")
        rewritten = self.db.define_feature(author_id="r", key="runnable", type="number", min=0, max=1,
                                           instructions="Share of deposits that could leave within days.",
                                           replaces=self.feature.id)
        self.assertEqual((rewritten.every, rewritten.start), ("quarter", "2023-03-31"))
        self.assertEqual(self.db.pending_work("runnable", [self.a.id], end="2023-06-30").total, 2)
        # A derived feature is computed whenever its inputs change, so a formula takes no schedule over.
        derived = self.db.define_feature(author_id="r", key="runnable", type="number", formula="0.5",
                                         instructions="Half.", replaces=rewritten.id)
        self.assertEqual((derived.every, derived.start), (None, None))

    def test_values_and_checks_mark_dates_done(self):
        self.db.schedule_feature("runnable", every="week", start="2023-01-02")
        first = self.value(self.at("2023-01-02"), self.a, 0.4)

        def todo(store=self.db, **options):
            work = store.pending_work("runnable", [self.a.id, self.b.id[:8], self.a.id], **options)
            return work.total, [(t.subject, t.date) for t in work.tasks]

        self.assertEqual(todo(end="2023-01-16"), (5, [("Bank A", "2023-01-09"), ("Bank A", "2023-01-16"),
                                                      ("Bank B", "2023-01-02"), ("Bank B", "2023-01-09"),
                                                      ("Bank B", "2023-01-16")]))
        check = self.at("2023-01-09").confirm_value(first.id[:8], author_id="agent")  # Read again; unchanged.
        self.assertEqual((check.value_id, check.subject_id, check.as_of), (first.id, self.a.id, "2023-01-09"))
        later = self.value(self.at("2023-01-16"), self.a, 0.6, updates=first)  # Read again; changed.
        self.assertEqual(todo(end="2023-01-16")[0], 3)
        self.assertEqual(todo(end="2023-01-23", start="2023-01-10"),
                         (3, [("Bank A", "2023-01-23"), ("Bank B", "2023-01-16"), ("Bank B", "2023-01-23")]))
        self.assertEqual(todo(end="2023-01-23", limit=1), (5, [("Bank A", "2023-01-23")]))
        # A reader at an earlier cutoff sees only what was done by then.
        self.assertEqual(todo(self.at("2023-01-09")), (2, [("Bank B", "2023-01-02"), ("Bank B", "2023-01-09")]))
        with self.assertRaisesRegex(ValueError, "after the cutoff"):
            todo(self.at("2023-01-09"), end="2023-01-16")
        # Only the latest value, dated before the check's date, is confirmed, once per date.
        for store, value, message in ((self.at("2023-01-23"), first, "is the latest value"),
                                      (self.at("2023-01-16"), later, "already stands for that date"),
                                      (self.at("2023-01-09"), first, "already confirmed")):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                store.confirm_value(value.id, author_id="agent")
        plain = self.db.create("Variable", text="x", author_id="agent", attributes={"key": "x", "value": 1})
        with self.assertRaisesRegex(ValueError, "not a feature value"):
            self.db.confirm_value(plain.id, author_id="agent")
        with self.assertRaisesRegex(ValueError, "name one or more Entities"):
            self.db.pending_work("runnable", [plain.id])

    def test_cli_lists_pending_dates_for_subjects_in_a_file(self):
        self.db.schedule_feature("runnable", every="month", start="2023-01-31")
        self.db.close()
        subjects = self.path.with_name("banks.txt")
        subjects.write_text(f"# banks\n{self.a.id}\n\n{self.b.id}\n", encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["--db", str(self.path), "pending", "runnable", "--subjects-file", str(subjects),
                                   "--to", "2023-03-31"]), 0)
        work = json.loads(out.getvalue())
        self.assertEqual((work["total"], [t["date"] for t in work["tasks"][:3]]), (6, ["2023-01-31", "2023-02-28", "2023-03-31"]))


if __name__ == "__main__":
    unittest.main()
