from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from premisedb import RecordStore


class StateTests(unittest.TestCase):
    """One Model per company and schema; Variables accumulate as versions; outputs are derived; the export
    gives change points."""

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.live = RecordStore(self.path)
        self.addCleanup(self.live.close)
        self.bank = self.live.create("Entity", text="Silicon Valley Bank", author_id="setup")
        self.other = self.live.create("Entity", text="Another Bank", author_id="setup")

    def at(self, date):
        store = RecordStore(self.path, as_of=date)
        self.addCleanup(store.close)
        return store

    def first_state(self, store, entity, deposits, uninsured, schema="bank-state-v1"):
        """The whole model, as a first session writes it: inputs with values, outputs null."""
        model = store.create("Model", text="State", author_id="agent", attributes={"schema": schema})
        store.create_link(model.id, entity.id, "about", author_id="agent")
        variables = {}
        for key, attributes in (
                ("total_deposits", {"role": "input", "value": deposits, "basis": "disclosed", "units": "USD millions"}),
                ("uninsured_deposits", {"role": "input", "value": uninsured, "basis": "disclosed"}),
                ("uninsured_share", {"role": "output", "value": None, "basis": "derived",
                                     "formula": "uninsured_deposits / total_deposits"}),
                ("deposits_by_quarter", {"role": "input", "value": [1, 2], "periods": ["2022-06-30", "2022-09-30"]}),
                ("deposit_change", {"role": "output", "value": None, "periods": ["2022-06-30", "2022-09-30"],
                                    "formula": "deposits_by_quarter / prev(deposits_by_quarter) - 1"})):
            variables[key] = store.create("Variable", text=key, author_id="agent", attributes={"key": key, **attributes})
            store.create_link(variables[key].id, model.id, "in_model", author_id="agent")
        for key in ("uninsured_deposits", "total_deposits"):
            store.create_link(variables["uninsured_share"].id, variables[key].id, "depends_on", author_id="agent")
        store.create_link(variables["deposit_change"].id, variables["deposits_by_quarter"].id, "depends_on", author_id="agent")
        return model, variables

    def new_version(self, store, model, previous, value):
        """What a later session writes for one changed field: a new Variable updating the current version."""
        version = store.create("Variable", text=previous.text, author_id="agent",
                               attributes=previous.attributes | {"value": value})
        store.create_link(version.id, model.id, "in_model", author_id="agent")
        store.create_link(version.id, previous.id, "updates", author_id="agent")
        return version

    def test_the_model_reads_the_latest_version_of_each_key_the_store_sees(self):
        then = self.at("2022-11-07")
        model, variables = self.first_state(then, self.bank, 176819, None)
        then.fill_model(model.id, author_id="agent")
        later = self.at("2023-02-24")
        self.new_version(later, model, variables["total_deposits"], 173109)
        self.new_version(later, model, variables["uninsured_deposits"], 165400)
        stored = lambda store, key: {v["key"]: v for v in store._model_variables(model.id)[1]}[key]
        self.assertEqual((stored(later, "total_deposits")["value"], stored(later, "total_deposits")["stamp"]),
                         (173109, "2023-02-24"))
        self.assertEqual((stored(then, "total_deposits")["value"], stored(then, "total_deposits")["stamp"]),
                         (176819, "2022-11-07"))
        self.assertEqual(stored(self.live, "uninsured_deposits")["value"], 165400)
        # The output's stored value is the 2022-11-07 one until it is filled again.
        check = {c.key: c for c in later.check_model(model.id).checked}
        self.assertEqual((check["uninsured_share"].stored, check["uninsured_share"].matches), (None, False))

    def test_fill_model_patches_what_it_may_edit_and_versions_the_rest(self):
        then = self.at("2022-11-07")
        model, variables = self.first_state(then, self.bank, 176819, None)
        filled = then.fill_model(model.id, author_id="agent")
        # The share is null from a null input, which already matches its stored null; the row is filled in place.
        self.assertEqual([(w.key, w.stored, w.value, w.id) for w in filled.written],
                         [("deposit_change", None, [None, 1.0], variables["deposit_change"].id)])
        self.assertEqual(then.fill_model(model.id, author_id="agent").written, ())
        later = self.at("2023-02-24")
        self.new_version(later, model, variables["total_deposits"], 173109)
        self.new_version(later, model, variables["uninsured_deposits"], 165400)
        filled = later.fill_model(model.id[:8], author_id="agent")
        self.assertEqual([(w.key, w.stored, w.value) for w in filled.written], [("uninsured_share", None, 165400 / 173109)])
        version = later.read(filled.written[0].id)
        self.assertNotEqual(version.id, variables["uninsured_share"].id)
        self.assertEqual((version.as_of, version.attributes["key"], version.attributes["formula"], version.attributes["value"]),
                         ("2023-02-24", "uninsured_share", "uninsured_deposits / total_deposits", 165400 / 173109))
        links = {(l.relation, l.to_id) for l in later.connections(version.id, direction="out").links}
        current = {v["key"]: v["id"] for v in later._model_variables(model.id)[1]}
        self.assertEqual(links, {("in_model", model.id), ("updates", variables["uninsured_share"].id),
                                 ("depends_on", current["total_deposits"]), ("depends_on", current["uninsured_deposits"])})
        self.assertTrue(all(c.exact for c in later.check_model(model.id).checked))
        # The earlier date still reads the earlier state, untouched.
        self.assertEqual({v["key"]: v["value"] for v in then._model_variables(model.id)[1]}["uninsured_share"], None)
        self.assertEqual(then.fill_model(model.id, author_id="agent").written, ())

    def test_fill_model_writes_nothing_when_a_formula_fails(self):
        model, variables = self.first_state(self.live, self.bank, 10, 5)
        self.live.patch(variables["uninsured_share"].id, author_id="agent", attributes={"formula": "uninsured_deposits +"})
        with self.assertRaisesRegex(ValueError, "uninsured_share"):
            self.live.fill_model(model.id, author_id="agent")
        self.assertIsNone(self.live.read(variables["uninsured_share"].id).attributes["value"])

    def test_state_rows_give_change_points_over_a_range(self):
        then = self.at("2022-11-07")
        model, variables = self.first_state(then, self.bank, 176819, None)
        then.fill_model(model.id, author_id="agent")
        later = self.at("2023-02-24")
        self.new_version(later, model, variables["total_deposits"], 173109)
        self.new_version(later, model, variables["uninsured_deposits"], 165400)
        later.fill_model(model.id, author_id="agent")
        other, _ = self.first_state(self.at("2023-01-15"), self.other, 100, 50)
        self.first_state(self.live, self.bank, 1, 1, schema="other-schema")
        self.live.create("Model", text="Not a state", author_id="agent")
        rows = self.live.state_rows(schema="bank-state-v1", start="2022-01-01", end="2023-12-31")
        self.assertEqual([(r.entity, r.date) for r in rows],
                         [("Another Bank", "2023-01-15"), ("Silicon Valley Bank", "2022-11-07"), ("Silicon Valley Bank", "2023-02-24")])
        first, second = rows[1], rows[2]
        self.assertEqual((first.values["total_deposits"], first.values["uninsured_share"]), (176819, None))
        self.assertEqual((second.values["total_deposits"], second.values["uninsured_share"]), (173109, 165400 / 173109))
        self.assertEqual(second.stamps, {"total_deposits": "2023-02-24", "uninsured_deposits": "2023-02-24",
                                         "uninsured_share": "2023-02-24", "deposits_by_quarter": "2022-11-07",
                                         "deposit_change": "2022-11-07"})
        self.assertEqual(second.periods["deposit_change"], ["2022-06-30", "2022-09-30"])
        # A single date gives the state entering it; today gives the current state.
        self.assertEqual([(r.date, r.values["total_deposits"]) for r in self.live.state_rows(entity_id=self.bank.id[:8], schema="bank-state-v1", start="2023-01-01", end="2023-01-01")],
                         [("2023-01-01", 176819)])
        self.assertEqual([r.date for r in self.live.state_rows(entity_id=self.bank.id, schema="bank-state-v1", start="2023-01-01")],
                         ["2023-01-01", "2023-02-24"])  # From a date to today.
        self.assertEqual([r.values["total_deposits"] for r in self.live.state_rows(entity_id=self.bank.id, schema="bank-state-v1")], [173109])
        self.assertEqual(self.live.state_rows(schema="bank-state-v1", start="2022-01-01", end="2022-06-30"), ())
        self.assertEqual({r.schema for r in self.live.state_rows()}, {"bank-state-v1", "other-schema"})
        # A reader at the first cutoff cannot look past it, and sees only the first state.
        self.assertEqual([r.date for r in then.state_rows(schema="bank-state-v1", start="2022-01-01")], ["2022-11-07"])
        with self.assertRaisesRegex(ValueError, "after the cutoff"):
            then.state_rows(end="2023-02-24")
        for bad in ({"schema": 3}, {"start": "2023-13-01"}, {"start": "2023-02-24", "end": "2023-02-23"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.live.state_rows(**bad)
