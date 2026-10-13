from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from premisedb import RecordStore


class StateTests(unittest.TestCase):
    """Filling a Model's outputs from its formulas, and exporting states written under a schema."""

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

    def state(self, store, entity, cutoff, deposits, uninsured, stored_share, schema="bank-state-v1"):
        model = store.create("Model", text=f"State as of {cutoff}", author_id="agent", attributes={"schema": schema})
        store.create_link(model.id, entity.id, "about", author_id="agent")
        variables = {}
        for key, attributes in (
                ("total_deposits", {"role": "input", "value": deposits, "basis": "disclosed"}),
                ("uninsured_deposits", {"role": "input", "value": uninsured, "basis": "disclosed"}),
                ("uninsured_share", {"role": "output", "value": stored_share,
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

    def test_fill_model_stores_exact_formula_values_and_nothing_else(self):
        then = self.at("2022-11-07")
        model, variables = self.state(then, self.bank, "2022-11-07", 176819, None, 0.9)
        with self.assertRaisesRegex(ValueError, "cannot be edited without a cutoff"):
            self.live.fill_model(model.id, author_id="agent")  # Written at a cutoff, filled only there.
        fill = then.fill_model(model.id, author_id="agent")
        self.assertEqual([(w.key, w.stored, w.value) for w in fill.written],
                         [("uninsured_share", 0.9, None), ("deposit_change", None, [None, 1.0])])
        self.assertIsNone(then.read(variables["uninsured_share"].id).attributes["value"])
        self.assertEqual(then.read(variables["total_deposits"].id).attributes["value"], 176819)
        self.assertEqual(then.fill_model(model.id[:8], author_id="agent").written, ())  # Already exact.
        check = then.check_model(model.id)
        self.assertTrue(all(c.exact for c in check.checked))

    def test_fill_model_writes_nothing_when_a_formula_fails(self):
        model, variables = self.state(self.live, self.bank, "today", 10, 5, 0.4)
        self.live.patch(variables["uninsured_share"].id, author_id="agent", attributes={"formula": "uninsured_deposits +"})
        with self.assertRaisesRegex(ValueError, "uninsured_share"):
            self.live.fill_model(model.id, author_id="agent")
        self.assertEqual(self.live.read(variables["uninsured_share"].id).attributes["value"], 0.4)

    def test_state_rows_export_one_row_per_entity_and_cutoff(self):
        self.state(self.at("2022-11-07"), self.bank, "2022-11-07", 176819, None, None)
        self.state(self.at("2023-02-24"), self.bank, "2023-02-24", 173109, 165400, 0.9554673644928917)
        self.state(self.at("2023-02-24"), self.other, "2023-02-24", 100, 50, 0.5)
        self.state(self.live, self.bank, "today", 1, 1, 1, schema="other-schema")
        self.live.create("Model", text="Not a state", author_id="agent")
        rows = self.live.state_rows(schema="bank-state-v1")
        self.assertEqual([(r.entity, r.cutoff) for r in rows],
                         [("Another Bank", "2023-02-24"), ("Silicon Valley Bank", "2022-11-07"), ("Silicon Valley Bank", "2023-02-24")])
        svb = rows[2]
        self.assertEqual((svb.entity_id, svb.schema), (self.bank.id, "bank-state-v1"))
        self.assertEqual(svb.values["uninsured_share"], 0.9554673644928917)
        self.assertEqual(svb.values["deposits_by_quarter"], [1, 2])
        self.assertEqual(svb.periods, {"deposits_by_quarter": ["2022-06-30", "2022-09-30"],
                                       "deposit_change": ["2022-06-30", "2022-09-30"]})
        self.assertEqual(rows[1].values["uninsured_deposits"], None)
        self.assertEqual({r.schema for r in self.live.state_rows()}, {"bank-state-v1", "other-schema"})
        self.assertEqual([r.cutoff for r in self.live.state_rows(entity_id=self.bank.id[:8], schema="bank-state-v1")],
                         ["2022-11-07", "2023-02-24"])
        # A reader at the first cutoff exports only the first state.
        self.assertEqual([(r.entity, r.cutoff) for r in self.at("2022-11-07").state_rows()],
                         [("Silicon Valley Bank", "2022-11-07")])
        with self.assertRaises(ValueError):
            self.live.state_rows(schema=3)
