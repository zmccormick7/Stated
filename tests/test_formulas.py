from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stated import RecordStore, evaluate
from stated.formulas import check_model, matches, parse


class FormulaLanguageTests(unittest.TestCase):
    def test_arithmetic_broadcasts_over_periods_and_null_propagates(self):
        self.assertEqual(evaluate("a * b + 1", {"a": [1, 2, None], "b": 2}), [3, 5, None])
        self.assertEqual(evaluate("-(a - b) / 2", {"a": 4, "b": 2}), -1)
        self.assertEqual(evaluate("a ** 2", {"a": [2, 3]}), [4, 9])
        self.assertEqual(evaluate("prev(a) * (1 + g)", {"a": [100, 110, 121], "g": 0.1}), [None, 110.00000000000001, 121.00000000000001])
        self.assertEqual(evaluate("prev(a, 90) * (1 + g)", {"a": [100], "g": [0.1]}), [99.00000000000001])
        self.assertEqual(evaluate("min(a, b)", {"a": [1, 5], "b": 3}), [1, 3])
        self.assertEqual(evaluate("max(a)", {"a": [1, 5, None]}), 5)
        self.assertEqual(evaluate("sum(a) / 2", {"a": [1, 5]}), 3)
        self.assertEqual(evaluate("abs(a)", {"a": [-1, 2]}), [1, 2])
        self.assertEqual(evaluate("a if a > b else b", {"a": [1, 5], "b": 3}), [3, 5])
        self.assertEqual(evaluate("1 if 0 < a <= 2 else 0", {"a": [1, 3]}), [1, 0])

    def test_language_boundary(self):
        for bad in ("__import__('os')", "a.b", "a[0]", "lambda: 1", "f(1)", "True", "a and b", "min(a, key=b)",
                    "'text'", "a = 1", "", "a +"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse(bad)
        self.assertEqual(parse("min(a, b) if c > 0 else prev(a)")[1], frozenset({"a", "b", "c"}))
        with self.assertRaisesRegex(ValueError, "unknown Variables: b"):
            evaluate("a + b", {"a": 1})
        with self.assertRaisesRegex(ValueError, "different numbers of periods"):
            evaluate("a + b", {"a": [1, 2], "b": [1]})
        with self.assertRaisesRegex(ValueError, "no numeric value"):
            evaluate("a + 1", {"a": "12"})
        with self.assertRaisesRegex(ValueError, "prev takes a list"):
            evaluate("prev(a)", {"a": 1})
        with self.assertRaises(ZeroDivisionError):
            evaluate("a / 0", {"a": 1})

    def test_lists_align_by_period_label_when_a_target_is_given(self):
        values = {"ramp": [1, 2, 3, 4, 5], "growth": [0.1, 0.2, 0.3], "flat": 10}
        periods = {"ramp": ["2025", "2026", "2027", "2028", "2029"], "growth": ["2027", "2028", "2029"]}
        self.assertEqual(evaluate("ramp * (1 + growth)", values, periods=periods, target=["2027", "2028", "2029"]),
                         [3.3000000000000003, 4.8, 6.5])
        self.assertEqual(evaluate("ramp * (1 + growth)", values, periods=periods, target=["2026", "2027"]),
                         [None, 3.3000000000000003])  # 2026 has no growth.
        self.assertEqual(evaluate("fill(growth, 0) * ramp", values, periods=periods, target=["2026", "2027"]), [0, 0.30000000000000004])
        self.assertEqual(evaluate('at(ramp, "2028") * flat', values, periods=periods, target=None), 40)
        self.assertEqual(evaluate('at(ramp, "2028") * flat', values, periods=periods, target=["2027", "2028"]), 40)
        # A row keeps its own periods through aggregates, at, prev and next; only combining reads it at the target.
        self.assertEqual(evaluate('sum(ramp) - at(ramp, "2025")', values, periods=periods, target=["2027"]), 14)
        self.assertEqual(evaluate('at(ramp * 2, "2027")', values, periods=periods, target=["2027", "2028"]), 6)
        self.assertEqual(evaluate("next(ramp) - ramp", values, periods=periods, target=["2028", "2029"]), [1, None])
        self.assertEqual(evaluate("next(ramp, 6) - ramp", values, periods=periods, target=["2028", "2029"]), [1, 1])
        self.assertEqual(evaluate("next(growth)", values, periods=periods, target=["2026", "2027"]), [None, 0.2])
        with self.assertRaisesRegex(ValueError, "different periods and no output periods"):
            evaluate("ramp * growth", values, periods=periods)
        self.assertEqual(evaluate("prev(ramp) * 2", values, periods=periods, target=["2027", "2028"]), [4, 6])
        with self.assertRaisesRegex(ValueError, "no period '2030'"):
            evaluate('at(ramp, "2030")', values, periods=periods)
        with self.assertRaisesRegex(ValueError, "periods are known"):
            evaluate('at(flat, "2027")', values, periods=periods)
        with self.assertRaisesRegex(ValueError, "do not match its values"):
            evaluate("ramp", values, periods={"ramp": ["2025"]}, target=["2025"])
        with self.assertRaisesRegex(ValueError, "distinct labels"):
            evaluate("ramp", values, periods=periods, target=["2025", "2025"])
        # Without labels, lists combine by position as before.
        with self.assertRaisesRegex(ValueError, "different numbers of periods"):
            evaluate("ramp * growth", values)
        for bad in ('at(ramp)', 'at(ramp, 2027)', 'at(ramp, growth)', '"2027"', 'fill(ramp)'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                evaluate(bad, values, periods=periods)

    def test_recurrences_compute_period_by_period(self):
        values = {"growth": [0.1, 0.2, 0.5], "start": 100}
        periods = {"growth": ["2026", "2027", "2028"]}
        target = ["2026", "2027", "2028"]
        self.assertEqual(evaluate("prev(revenue, start) * (1 + growth)", values, periods=periods, target=target,
                                  self_key="revenue"), [110.00000000000001, 132.0, 198.0])
        self.assertEqual(evaluate("fill(prev(revenue), start) + 1", values, periods=periods, target=target,
                                  self_key="revenue"), [101, 102, 103])
        with self.assertRaisesRegex(ValueError, "only inside prev"):
            evaluate("revenue * growth", values, periods=periods, target=target, self_key="revenue")
        with self.assertRaisesRegex(ValueError, "needs the output to have periods"):
            evaluate("prev(revenue, start)", values, periods=periods, self_key="revenue")
        with self.assertRaisesRegex(ValueError, "value for each period"):
            evaluate("sum(prev(revenue, start))", values, periods=periods, target=target, self_key="revenue")

    def test_matches_uses_a_relative_tolerance_above_one(self):
        self.assertTrue(matches(13.6547, 13.65472, tolerance=1e-4))
        self.assertFalse(matches(13.6547, 13.66, tolerance=1e-4))
        self.assertTrue(matches(0.8591, 0.85915, tolerance=1e-4))  # Absolute below 1.
        self.assertTrue(matches([1, None], [1.00001, None], tolerance=1e-4))
        self.assertFalse(matches([1, None], [1, 2], tolerance=1e-4))
        self.assertFalse(matches(1, [1], tolerance=1e-4))
        self.assertFalse(matches("1", 1, tolerance=1e-4))


class _ModelCase(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = RecordStore(Path(directory.name) / "research.sqlite")
        self.addCleanup(self.db.close)
        self.model = self.db.create("Model", text="PowerFrame revenue model", author_id="a")
        self.variables = {}

    def variable(self, key, role, value, formula=None, depends_on=(), model=None, periods=None):
        attributes = {"key": key, "role": role, "value": value}
        if periods is not None:
            attributes["periods"] = periods
        if formula is not None:
            attributes["formula"] = formula
        record = self.db.create("Variable", text=key, author_id="a", attributes=attributes)
        self.db.create_link(record.id, (model or self.model).id, "in_model", author_id="a")
        for dependency in depends_on:
            self.db.create_link(record.id, self.variables[dependency].id, "depends_on", author_id="a")
        self.variables[key] = record
        return record


class ModelCheckTests(_ModelCase):
    def test_outputs_are_checked_from_stored_values(self):
        self.variable("gw", "input", [20, 20, 24])
        self.variable("price", "input", 570)
        self.variable("growth", "input", [None, 0.1, 0.1])
        self.variable("revenue", "output", [11400, 11400, 13680], "gw * price", ["gw", "price"])
        self.variable("price_path", "output", [570, 627, 689.7], "prev(price_path, price) * (1 + fill(growth, 0))",
                      ["price", "growth"], periods=["2026", "2027", "2028"])
        self.variable("selfish", "output", 1, "gw * selfish", ["gw"])
        self.variable("wrong", "output", 1, "gw * price", ["gw", "price"])
        self.variable("undeclared", "output", 11400, "gw * price", ["gw"])
        self.variable("unknown", "output", 1, "gw * tariff", ["gw"])
        self.variable("broken", "output", 1, "gw +", [])
        self.variable("manual", "output", 5)
        result = self.db.check_model(self.model.id[:8])
        self.assertEqual((result.model_id, result.inputs, result.outputs, result.without_formula),
                         (self.model.id, 3, 8, ("manual",)))
        by_key = {check.key: check for check in result.checked}
        self.assertEqual(by_key["revenue"].computed, [11400, 11400, 13680])
        self.assertTrue(by_key["revenue"].matches)
        self.assertTrue(by_key["price_path"].matches)  # A recurrence over its own earlier periods.
        self.assertEqual(by_key["selfish"].problems, ("a formula that refers to its own Variable needs the output to have periods",))
        self.assertEqual((by_key["wrong"].matches, by_key["wrong"].computed, by_key["wrong"].problems),
                         (False, [11400, 11400, 13680], ("stored value differs from the formula's result",)))
        self.assertEqual(by_key["undeclared"].problems, ("names Variables without a depends_on link: price",))
        self.assertEqual(by_key["unknown"].problems, ("names Variables not in the model: tariff",))
        self.assertIn("syntax", by_key["broken"].problems[0])
        self.assertEqual([check.key for check in result.checked],
                         ["revenue", "price_path", "selfish", "wrong", "undeclared", "unknown", "broken"])

    def test_a_null_value_yields_null_and_stops_nothing_else(self):
        self.variable("a", "input", 2)
        self.variable("b", "input", None)  # Undisclosed: an unknown value, not zero.
        self.variable("c", "output", 4, "a * 2", ["a"])
        self.variable("d", "output", None, "b * 2", ["b"])
        self.variable("brow", "input", [None, 2], periods=["2026", "2027"])
        self.variable("e", "output", [None, 4], "brow * 2", ["brow"], periods=["2026", "2027"])
        self.variable("rounded", "output", 0.6667, "a / 3", ["a"])
        by_key = {check.key: check for check in self.db.check_model(self.model.id).checked}
        self.assertEqual((by_key["c"].matches, by_key["c"].exact, by_key["c"].computed), (True, True, 4))
        self.assertEqual((by_key["d"].matches, by_key["d"].exact, by_key["d"].computed), (True, True, None))
        self.assertEqual((by_key["e"].computed, by_key["e"].exact), ([None, 4], True))
        self.assertEqual((by_key["rounded"].matches, by_key["rounded"].exact, by_key["rounded"].problems),
                         (True, False, ()))

    def test_dependencies_on_other_models_do_not_count_as_declarations(self):
        other = self.db.create("Model", text="Other", author_id="a")
        self.variable("capex", "input", 100, model=other)
        self.variable("capex", "input", 100)  # Same key, this model; the other model's is ignored.
        self.variable("spend", "output", 50, "capex / 2", ["capex"])
        result = self.db.check_model(self.model.id)
        self.assertTrue(result.checked[0].matches)
        self.assertEqual(result.inputs, 1)

    def test_invalid_requests(self):
        with self.assertRaisesRegex(ValueError, "not a Model"):
            self.db.check_model(self.db.create("Finding", text="x", author_id="a").id)
        with self.assertRaises(KeyError):
            self.db.check_model("missing-id")
        for tolerance in (-1, 1, "0.1", True):
            with self.subTest(tolerance=tolerance), self.assertRaises(ValueError):
                self.db.check_model(self.model.id, tolerance=tolerance)
        self.variable("a", "input", 1)
        self.variable("double", "output", 4, "a * 2", ["a"])
        # A second Variable with the same key is a later version of it; the model reads the latest.
        self.db.create_link(self.db.create("Variable", text="a", author_id="a", attributes={"key": "a", "role": "input", "value": 2}).id,
                            self.model.id, "in_model", author_id="a")
        self.assertEqual([(c.key, c.computed) for c in self.db.check_model(self.model.id).checked], [("double", 4)])
        self.assertEqual(self.db.check_model(self.db.create("Model", text="Empty", author_id="a").id).checked, ())


class FillModelTests(_ModelCase):
    def build(self):
        self.variable("gw", "input", [20, 20, 24])
        self.variable("price", "input", 570)
        self.variable("margin", "input", 0.25)
        self.variable("revenue", "output", [11400, 11400, 13680], "gw * price", ["gw", "price"])
        self.variable("manual", "output", 5)
        # Created before the output it reads, and stored stale: filling computes it in dependency order.
        profit = self.db.create("Variable", text="profit", author_id="a", attributes={
            "key": "profit", "role": "output", "value": [0, 0, 0], "formula": "revenue * margin + manual"})
        self.db.create_link(profit.id, self.model.id, "in_model", author_id="a")
        for key in ("revenue", "margin", "manual"):
            self.db.create_link(profit.id, self.variables[key].id, "depends_on", author_id="a")

    def test_results_flow_through_dependent_formulas(self):
        self.build()
        filled = self.db.fill_model(self.model.id[:8], author_id="a")
        self.assertEqual([(f.key, f.stored, f.value) for f in filled.written], [("profit", [0, 0, 0], [2855.0, 2855.0, 3425.0])])
        self.assertTrue(all(c.exact for c in self.db.check_model(self.model.id).checked))
        self.assertEqual(self.db.fill_model(self.model.id, author_id="a").written, ())

    def test_what_cannot_be_filled(self):
        with self.assertRaisesRegex(ValueError, "not a Model, Entity or Variable"):
            self.db.fill_model(self.db.create("Finding", text="q", author_id="a").id, author_id="a")
        self.variable("a", "output", 1, "b + 1")
        self.variable("b", "output", 1, "a + 1")
        with self.assertRaisesRegex(ValueError, "cycle: a, b"):
            self.db.fill_model(self.model.id, author_id="a")
        other = self.db.create("Model", text="Other", author_id="a")
        self.variable("ratio", "output", 1, "gw / tariff", model=other)
        with self.assertRaisesRegex(ValueError, "ratio: formula names unknown Variables: gw, tariff"):
            self.db.fill_model(other.id, author_id="a")


class DynamicModelTests(unittest.TestCase):
    """A company's model maintained at each cutoff: every read at a date sees the model as it stood then."""

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"

    def at(self, day):
        store = RecordStore(self.path, as_of=day)
        self.addCleanup(store.close)
        return store

    def test_inputs_drivers_and_formulas_change_as_of_their_dates(self):
        first = self.at("2023-02-15")
        model = first.contribute(author_id="agent", records=[
            {"ref": "m", "kind": "Model", "text": "Memory maker revenue: bits shipped times price."},
            {"ref": "bits", "kind": "Variable", "text": "DRAM bits", "attributes": {"key": "dram_bits", "role": "input", "value": 10}},
            {"ref": "price", "kind": "Variable", "text": "DRAM price", "attributes": {"key": "dram_price", "role": "input", "value": 3}},
            {"ref": "rev", "kind": "Variable", "text": "Revenue", "attributes": {
                "key": "revenue", "role": "output", "value": 30, "formula": "dram_bits * dram_price"}}],
            links=[{"from_id": r, "to_id": "m", "relation": "in_model"} for r in ("bits", "price", "rev")]
                  + [{"from_id": "rev", "to_id": r, "relation": "depends_on"} for r in ("bits", "price")]).records
        later = self.at("2024-05-15")
        # The company starts reporting a new product line: a driver is added, the formula changes, a price moves
        # and an old driver is dropped, each as of this date.
        hbm = later.contribute(author_id="agent", records=[{"ref": "h", "kind": "Variable", "text": "HBM revenue",
            "attributes": {"key": "hbm_revenue", "role": "input", "value": 12}}],
            links=[{"from_id": "h", "to_id": model["m"].id, "relation": "in_model"}]).records["h"]
        old_formula = model["rev"].id
        rev = later.patch(old_formula, author_id="agent", attributes={"formula": "dram_bits * dram_price + hbm_revenue"})
        self.assertNotEqual(rev.id, old_formula)  # A changed formula is a new version, linked `updates` the old.
        later.create_link(rev.id, hbm.id, "depends_on", author_id="agent")
        later.patch(model["price"].id, author_id="agent", attributes={"value": 4})
        dropped = later.contribute(author_id="agent", records=[{"ref": "x", "kind": "Variable", "text": "Legacy NOR",
            "attributes": {"key": "nor_bits", "role": "input", "value": 1}}],
            links=[{"from_id": "x", "to_id": model["m"].id, "relation": "in_model"}]).records["x"]
        filled = later.fill_model(model["m"].id, author_id="agent")
        self.assertEqual([(f.key, f.value) for f in filled.written], [("revenue", 52)])
        retired = self.at("2024-08-15").patch(dropped.id, author_id="agent", attributes={"retired": True})
        self.assertNotEqual(retired.id, dropped.id)

        def model_at(day):
            check = self.at(day).check_model(model["m"].id)
            return check.inputs, [(c.key, c.stored, c.matches) for c in check.checked]

        self.assertEqual(model_at("2023-06-30"), (2, [("revenue", 30, True)]))
        self.assertEqual(model_at("2024-06-30"), (4, [("revenue", 52, True)]))
        self.assertEqual(model_at("2024-09-30")[0], 3)  # The dropped driver is gone from this date on.
        self.assertEqual(self.at("2023-06-30").read(old_formula).attributes["formula"], "dram_bits * dram_price")

    def test_bringing_a_retired_driver_back_is_a_version_too(self):
        store = RecordStore(self.path)
        self.addCleanup(store.close)
        variable = store.create("Variable", text="x", author_id="a", attributes={"key": "x", "value": 1})
        retired = store.patch(variable.id, author_id="a", attributes={"retired": True})
        back = store.patch(retired.id, author_id="a", remove=["retired"])
        self.assertEqual(len({variable.id, retired.id, back.id}), 3)
        self.assertEqual(store.patch(back.id, author_id="a", text="x, renamed").id, back.id)  # Text edits in place.
