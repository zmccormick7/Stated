from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from premisedb import RecordStore


class FeatureTests(unittest.TestCase):
    """A feature value is a Variable about an Entity; formulas resolve through depends_on; values accumulate as
    versions; the export gives change points per Entity."""

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.live = RecordStore(self.path)
        self.addCleanup(self.live.close)
        self.bank = self.live.create("Entity", text="Silicon Valley Bank", author_id="setup")
        self.other = self.live.create("Entity", text="Another Bank", author_id="setup")
        formulas = {"uninsured_share": "uninsured_deposits / total_deposits",
                    "deposit_change": "deposits_by_quarter / prev(deposits_by_quarter) - 1"}
        self.definitions = {key: self.live.define_feature(author_id="setup", key=key, type="number", formula=formulas.get(key),
                                                          instructions=f"The bank's {key}.")
                            for key in ("total_deposits", "uninsured_deposits", "uninsured_share", "deposits_by_quarter",
                                        "deposit_change")}

    def at(self, date):
        store = RecordStore(self.path, as_of=date)
        self.addCleanup(store.close)
        return store

    def feature(self, store, entity, key, value, *, formula=None, inputs=(), periods=None, updates=None):
        attributes = {"key": key, "feature": self.definitions[key].id, "value": value,
                      "basis": "derived" if formula else "disclosed"}
        if formula:
            attributes["formula"] = formula
        if periods:
            attributes["periods"] = periods
        variable = store.create("Variable", text=key, author_id="agent", attributes=attributes)
        store.create_link(variable.id, entity.id, "about", author_id="agent")
        for target in inputs:
            store.create_link(variable.id, target.id, "depends_on", author_id="agent")
        if updates is not None:
            store.create_link(variable.id, updates.id, "updates", author_id="agent")
        return variable

    def first_state(self, store, entity, deposits, uninsured):
        """What a first session writes: disclosed inputs and derived features with null values."""
        total = self.feature(store, entity, "total_deposits", deposits)
        uninsured_ = self.feature(store, entity, "uninsured_deposits", uninsured)
        share = self.feature(store, entity, "uninsured_share", None, formula="uninsured_deposits / total_deposits",
                             inputs=(total, uninsured_))
        row = self.feature(store, entity, "deposits_by_quarter", [1, 2], periods=["2022-06-30", "2022-09-30"])
        change = self.feature(store, entity, "deposit_change", None, formula="deposits_by_quarter / prev(deposits_by_quarter) - 1",
                              inputs=(row,), periods=["2022-06-30", "2022-09-30"])
        return {"total_deposits": total, "uninsured_deposits": uninsured_, "uninsured_share": share,
                "deposits_by_quarter": row, "deposit_change": change}

    def test_a_feature_is_checked_against_what_it_depends_on(self):
        then = self.at("2022-11-07")
        f = self.first_state(then, self.bank, 176819, 150000)
        check = then.check_model(f["uninsured_share"].id)
        self.assertEqual([(c.key, c.stored, c.computed, c.matches, c.problems) for c in check.checked],
                         [("uninsured_share", None, 150000 / 176819, False, ("stored value differs from the formula's result",))])
        then.fill_model(f["uninsured_share"].id, author_id="agent")
        self.assertTrue(then.check_model(f["uninsured_share"].id).checked[0].exact)
        self.assertEqual(then.check_model(f["uninsured_share"].id).checked[0].stored, 150000 / 176819)
        # A feature whose dependency links are missing is reported, not guessed.
        loose = then.create("Variable", text="loose", author_id="agent", attributes={"key": "loose", "formula": "total_deposits * 2", "value": 1})
        then.create_link(loose.id, self.bank.id, "about", author_id="agent")
        self.assertEqual(then.check_model(loose.id).checked[0].problems, ("names Variables not in the model: total_deposits",))
        with self.assertRaisesRegex(ValueError, "not a Model, Entity or Variable"):
            then.check_model(then.create("Question", text="Which bank?", author_id="agent").id)

    def test_fill_model_on_an_entity_derives_every_feature_from_the_latest_inputs(self):
        then = self.at("2022-11-07")
        f = self.first_state(then, self.bank, 176819, 150000)
        filled = then.fill_model(self.bank.id, author_id="agent")
        self.assertEqual([(w.key, w.stored, w.id) for w in filled.written],
                         [("uninsured_share", None, f["uninsured_share"].id), ("deposit_change", None, f["deposit_change"].id)])
        self.assertEqual(then.read(f["uninsured_share"].id).attributes["value"], 150000 / 176819)  # Filled in place.
        self.assertEqual(then.fill_model(self.bank.id, author_id="agent").written, ())
        # A later date brings new inputs; the derived feature gets a new version resting on them.
        later = self.at("2023-02-24")
        total = self.feature(later, self.bank, "total_deposits", 173109, updates=f["total_deposits"])
        uninsured = self.feature(later, self.bank, "uninsured_deposits", 165400, updates=f["uninsured_deposits"])
        filled = later.fill_model(self.bank.id, author_id="agent")
        self.assertEqual([(w.key, w.stored, w.value) for w in filled.written], [("uninsured_share", 150000 / 176819, 165400 / 173109)])
        version = later.read(filled.written[0].id)
        self.assertNotEqual(version.id, f["uninsured_share"].id)
        self.assertEqual((version.as_of, version.attributes["key"], version.attributes["value"]), ("2023-02-24", "uninsured_share", 165400 / 173109))
        self.assertEqual({(l.relation, l.to_id) for l in later.connections(version.id, direction="out").links},
                         {("about", self.bank.id), ("updates", f["uninsured_share"].id), ("depends_on", total.id), ("depends_on", uninsured.id)})
        self.assertTrue(all(c.exact for c in later.check_model(self.bank.id).checked))
        # The earlier date reads the earlier state, untouched.
        self.assertEqual(then.read(f["uninsured_share"].id).attributes["value"], 150000 / 176819)
        self.assertEqual(then.fill_model(self.bank.id, author_id="agent").written, ())
        self.assertEqual({v.key: v.value for v in later.evaluate_model(self.bank.id, {"total_deposits": 200000}).variables}["uninsured_share"], 165400 / 200000)

    def test_state_rows_give_change_points_per_entity(self):
        then = self.at("2022-11-07")
        f = self.first_state(then, self.bank, 176819, None)
        then.fill_model(self.bank.id, author_id="agent")
        later = self.at("2023-02-24")
        self.feature(later, self.bank, "total_deposits", 173109, updates=f["total_deposits"])
        self.feature(later, self.bank, "uninsured_deposits", 165400, updates=f["uninsured_deposits"])
        later.fill_model(self.bank.id, author_id="agent")
        self.first_state(self.at("2023-01-15"), self.other, 100, 50)
        self.live.create("Variable", text="Not a feature", author_id="agent", attributes={"key": "x", "value": 1})
        rows = self.live.state_rows(start="2022-01-01", end="2023-12-31")
        self.assertEqual([(r.entity, r.date) for r in rows],
                         [("Another Bank", "2023-01-15"), ("Silicon Valley Bank", "2022-11-07"), ("Silicon Valley Bank", "2023-02-24")])
        first, second = rows[1], rows[2]
        self.assertEqual((first.entity_id, first.values["total_deposits"], first.values["uninsured_share"]), (self.bank.id, 176819, None))
        self.assertEqual((second.values["total_deposits"], second.values["uninsured_share"]), (173109, 165400 / 173109))
        self.assertEqual(second.stamps, {"total_deposits": "2023-02-24", "uninsured_deposits": "2023-02-24",
                                         "uninsured_share": "2023-02-24", "deposits_by_quarter": "2022-11-07",
                                         "deposit_change": "2022-11-07"})
        self.assertEqual(second.periods["deposit_change"], ["2022-06-30", "2022-09-30"])
        self.assertEqual([(r.date, r.values) for r in self.live.state_rows(entity_id=self.bank.id[:8], features=["uninsured_share"], start="2023-01-01")],
                         [("2023-01-01", {"uninsured_share": None}), ("2023-02-24", {"uninsured_share": 165400 / 173109})])
        self.assertEqual([r.values["total_deposits"] for r in self.live.state_rows(entity_id=self.bank.id)], [173109])
        self.assertEqual(self.live.state_rows(start="2022-01-01", end="2022-06-30"), ())
        self.assertEqual([r.date for r in then.state_rows(entity_id=self.bank.id, start="2022-01-01")], ["2022-11-07"])
        with self.assertRaisesRegex(ValueError, "after the cutoff"):
            then.state_rows(end="2023-02-24")
        for bad in ({"features": "x"}, {"features": []}, {"start": "2023-13-01"}, {"start": "2023-02-24", "end": "2023-02-23"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.live.state_rows(**bad)
