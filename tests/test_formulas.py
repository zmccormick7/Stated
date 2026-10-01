from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from premisedb import RecordStore, evaluate
from premisedb.formulas import check_model, matches, parse


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

    def test_matches_uses_a_relative_tolerance_above_one(self):
        self.assertTrue(matches(13.6547, 13.65472, tolerance=1e-4))
        self.assertFalse(matches(13.6547, 13.66, tolerance=1e-4))
        self.assertTrue(matches(0.8591, 0.85915, tolerance=1e-4))  # Absolute below 1.
        self.assertTrue(matches([1, None], [1.00001, None], tolerance=1e-4))
        self.assertFalse(matches([1, None], [1, 2], tolerance=1e-4))
        self.assertFalse(matches(1, [1], tolerance=1e-4))
        self.assertFalse(matches("1", 1, tolerance=1e-4))


class ModelCheckTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = RecordStore(Path(directory.name) / "research.sqlite")
        self.addCleanup(self.db.close)
        self.model = self.db.create("Model", text="PowerFrame revenue model", author_id="a")
        self.variables = {}

    def variable(self, key, role, value, formula=None, depends_on=(), model=None):
        attributes = {"key": key, "role": role, "value": value}
        if formula is not None:
            attributes["formula"] = formula
        record = self.db.create("Variable", text=key, author_id="a", attributes=attributes)
        self.db.create_link(record.id, (model or self.model).id, "in_model", author_id="a")
        for dependency in depends_on:
            self.db.create_link(record.id, self.variables[dependency].id, "depends_on", author_id="a")
        self.variables[key] = record
        return record

    def test_outputs_are_checked_from_stored_values(self):
        self.variable("gw", "input", [20, 20, 24])
        self.variable("price", "input", 570)
        self.variable("growth", "input", [None, 0.1, 0.1])
        self.variable("revenue", "output", [11400, 11400, 13680], "gw * price", ["gw", "price"])
        self.variable("price_path", "output", [None, 627, 627], "prev(price_path, price) * (1 + growth)", ["price", "growth"])
        self.variable("wrong", "output", 1, "gw * price", ["gw", "price"])
        self.variable("undeclared", "output", 11400, "gw * price", ["gw"])
        self.variable("unknown", "output", 1, "gw * tariff", ["gw"])
        self.variable("broken", "output", 1, "gw +", [])
        self.variable("manual", "output", 5)
        result = self.db.check_model(self.model.id[:8])
        self.assertEqual((result.model_id, result.inputs, result.outputs, result.without_formula),
                         (self.model.id, 3, 7, ("manual",)))
        by_key = {check.key: check for check in result.checked}
        self.assertEqual(by_key["revenue"].computed, [11400, 11400, 13680])
        self.assertTrue(by_key["revenue"].matches)
        self.assertEqual(by_key["price_path"].problems, ("formula refers to its own Variable",))
        self.assertEqual((by_key["wrong"].matches, by_key["wrong"].computed, by_key["wrong"].problems),
                         (False, [11400, 11400, 13680], ("stored value differs from the formula's result",)))
        self.assertEqual(by_key["undeclared"].problems, ("names Variables without a depends_on link: price",))
        self.assertEqual(by_key["unknown"].problems, ("names Variables not in the model: tariff",))
        self.assertIn("syntax", by_key["broken"].problems[0])
        self.assertEqual([check.key for check in result.checked],
                         ["revenue", "price_path", "wrong", "undeclared", "unknown", "broken"])

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
        self.db.create("Variable", text="dup", author_id="a", attributes={"key": "a", "role": "input"})
        self.db.create_link(self.db.search_records("dup")[0].id, self.model.id, "in_model", author_id="a")
        with self.assertRaisesRegex(ValueError, "share the key"):
            self.db.check_model(self.model.id)
        self.assertEqual(self.db.check_model(self.db.create("Model", text="Empty", author_id="a").id).checked, ())
