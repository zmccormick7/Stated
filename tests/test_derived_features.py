from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stated import RecordStore
from stated.feature_runs import derive_features


class DerivedFeatureTests(unittest.TestCase):
    """Derived features are computed mechanically, date by date, from the latest input values recorded as
    of each date; computing again after more inputs are written brings them in line."""

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        with RecordStore(self.path) as db:
            self.bank = db.create("Entity", text="Bank", author_id="setup").id
            define = lambda key, type, **more: db.define_feature(author_id="r", key=key, type=type,
                                                                 instructions=f"The {key}.", **more)
            self.inputs = {key: define(key, "number", min=0) for key in ("uninsured", "total")}
            self.share = define("share", "number", max=1, formula="uninsured / total", units="fraction")
            self.high = define("high_share", "boolean", formula="share > 0.5")
        self.values = {}

    def value(self, day, key, value, period):
        with RecordStore(self.path, as_of=day) as db:
            made = db.contribute(author_id="agent", records=[{"ref": "v", "kind": "Variable", "text": key,
                "attributes": {"key": key, "feature": self.inputs[key].id, "value": value, "period": period}}],
                links=[{"from_id": "v", "to_id": self.bank, "relation": "about"}]).records["v"]
        self.values[(day, key)] = made.id

    def derive(self):
        return [(d.date, d.key, d.value, d.id is not None, d.problem)
                for d in derive_features(self.path, [self.bank], author_id="derive")]

    def series(self, key):
        with RecordStore(self.path) as db:
            return [(r.date, r.values[key]) for r in db.state_rows(entity_id=self.bank, features=[key], start="2023-01-01",
                                                                   end="2023-12-31")]

    def test_derived_values_follow_their_inputs_date_by_date(self):
        self.value("2023-01-02", "total", 100, "2022-09-30")
        self.value("2023-01-09", "uninsured", 60, "2022-09-30")
        self.value("2023-01-16", "total", 120, "2022-12-31")
        self.value("2023-01-23", "uninsured", 90, "2022-12-31")
        self.assertEqual(self.derive(), [
            ("2023-01-02", "share", None, False, "no value of uninsured about this subject yet"),
            ("2023-01-02", "high_share", None, False, "no value of share about this subject yet"),
            ("2023-01-09", "share", 0.6, True, None),
            ("2023-01-09", "high_share", True, True, None),
            ("2023-01-16", "share", None, False, "its inputs are for different periods: 2022-09-30, 2022-12-31"),
            ("2023-01-23", "share", 0.75, True, None)])  # high_share is still true: nothing to write.
        with RecordStore(self.path) as db:
            latest = db.list_records(kind="Variable", where={"feature": self.share.id}).records[-1]
            self.assertEqual((latest.as_of, latest.attributes["period"], latest.attributes["units"],
                              latest.attributes["formula"]), ("2023-01-23", "2022-12-31", "fraction", "uninsured / total"))
            out = {(l.relation, l.to_id) for l in db.connections(latest.id, direction="out").links}
            self.assertTrue({("about", self.bank), ("depends_on", self.values[("2023-01-23", "uninsured")]),
                             ("depends_on", self.values[("2023-01-16", "total")])} <= out)
            self.assertIn("updates", {relation for relation, _ in out})
        self.assertEqual([d for d in self.derive() if d[3]], [])  # Nothing new: computing again writes nothing.
        # An input written later for an earlier date: the dates after it are computed again.
        self.value("2023-01-05", "total", 200, "2022-09-30")
        self.assertEqual([d for d in self.derive() if d[3]], [("2023-01-09", "share", 0.3, True, None),
                                                            ("2023-01-09", "high_share", False, True, None),
                                                            ("2023-01-23", "high_share", True, True, None)])
        self.assertEqual(self.series("share"), [("2023-01-09", 0.3), ("2023-01-23", 0.75)])
        self.assertEqual(self.series("high_share"), [("2023-01-09", False), ("2023-01-23", True)])

    def test_a_value_outside_the_type_is_reported_and_loops_refused(self):
        self.value("2023-01-02", "total", 100, None)
        self.value("2023-01-02", "uninsured", 150, None)
        self.assertEqual(self.derive()[0][:4], ("2023-01-02", "share", None, False))
        self.assertIn("not a value of feature share", self.derive()[0][4])
        with RecordStore(self.path) as db:
            db.define_feature(author_id="r", key="a", type="number", formula="b + 1", instructions="A.")
            db.define_feature(author_id="r", key="b", type="number", formula="a + 1", instructions="B.")
        with self.assertRaisesRegex(ValueError, "in a loop"):
            self.derive()


if __name__ == "__main__":
    unittest.main()
