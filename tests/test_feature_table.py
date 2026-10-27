import contextlib
import csv
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stated import RecordStore
from stated.cli import main


class FeatureTableTests(unittest.TestCase):
    """The daily feature table and the change-point export read values by definition, forward-filled, with
    the date each value is dated and the latest date it was read."""

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        with RecordStore(self.path) as db:
            self.bank = db.create("Entity", text="Bank", author_id="setup").id
            self.share = db.define_feature(author_id="r", key="share", type="number", instructions="The share.")
            self.cut = db.define_feature(author_id="r", key="cut", type="boolean", instructions="Dividend cut.")
            # A driver model's Variable sharing the key is not a value of the feature.
            model = db.create("Variable", text="share", author_id="model", attributes={"key": "share", "value": 99})
            db.create_link(model.id, self.bank, "about", author_id="model")

    def value(self, day, definition, value):
        with RecordStore(self.path, as_of=day) as db:
            return db.contribute(author_id="agent", records=[{"ref": "v", "kind": "Variable", "text": "v",
                "attributes": {"key": definition.key, "feature": definition.id, "value": value}}],
                links=[{"from_id": "v", "to_id": self.bank, "relation": "about"}]).records["v"]

    def test_the_table_forward_fills_values_with_when_they_were_dated_and_read(self):
        first = self.value("2023-01-03", self.share, 0.4)
        with RecordStore(self.path, as_of="2023-01-05") as db:
            db.confirm_value(first.id, author_id="agent")
        self.value("2023-01-05", self.cut, True)
        self.value("2023-01-06", self.share, 0.6)
        with RecordStore(self.path) as db:
            table = db.feature_table(start="2023-01-02", end="2023-01-09", every="weekday")
            self.assertEqual(table.columns, ("subject_id", "subject", "date", "cut", "cut_as_of", "cut_checked",
                                             "share", "share_as_of", "share_checked"))
            self.assertEqual([row[2:] for row in table.rows], [
                ("2023-01-02", None, None, None, None, None, None),
                ("2023-01-03", None, None, None, 0.4, "2023-01-03", "2023-01-03"),
                ("2023-01-04", None, None, None, 0.4, "2023-01-03", "2023-01-03"),
                ("2023-01-05", True, "2023-01-05", "2023-01-05", 0.4, "2023-01-03", "2023-01-05"),
                ("2023-01-06", True, "2023-01-05", "2023-01-05", 0.6, "2023-01-06", "2023-01-06"),
                ("2023-01-09", True, "2023-01-05", "2023-01-05", 0.6, "2023-01-06", "2023-01-06")])
            self.assertEqual(len(db.feature_table(["share"], [self.bank], start="2023-01-01", end="2023-01-09").rows), 9)
            self.assertEqual([(r.date, r.values) for r in db.state_rows(start="2023-01-01", end="2023-01-09")],
                             [("2023-01-03", {"share": 0.4}), ("2023-01-05", {"share": 0.4, "cut": True}),
                              ("2023-01-06", {"share": 0.6, "cut": True})])
            for options, message in (({"every": "hour"}, "day or weekday"), ({"end": "2022-12-31"}, "not be after"),
                                     ({"features": [self.share.id, "share"]}, "one definition per key")):
                with self.subTest(options=options), self.assertRaisesRegex(ValueError, message):
                    db.feature_table(**{"start": "2023-01-02", **options})
        with RecordStore(self.path, as_of="2023-01-05") as then:
            self.assertEqual(then.feature_table(["share"], start="2023-01-05").rows[0][3:],
                             (0.4, "2023-01-03", "2023-01-05"))
            with self.assertRaisesRegex(ValueError, "after the cutoff"):
                then.feature_table(start="2023-01-05", end="2023-01-06")

    def test_cli_writes_the_table_as_csv(self):
        self.value("2023-01-03", self.cut, False)
        out = self.path.with_name("table.csv")
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            self.assertEqual(main(["--db", str(self.path), "feature-table", "--feature", "cut", "--subject",
                                   self.bank[:8], "--from", "2023-01-02", "--to", "2023-01-03", "--out", str(out)]), 0)
        self.assertEqual(json.loads(printed.getvalue())["rows"], 2)
        with open(out, newline="", encoding="utf-8") as file:
            rows = list(csv.reader(file))
        self.assertEqual(rows, [["subject_id", "subject", "date", "cut", "cut_as_of", "cut_checked"],
                                [self.bank, "Bank", "2023-01-02", "", "", ""],
                                [self.bank, "Bank", "2023-01-03", "false", "2023-01-03", "2023-01-03"]])


if __name__ == "__main__":
    unittest.main()
