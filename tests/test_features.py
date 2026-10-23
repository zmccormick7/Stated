from pathlib import Path
import json
from tempfile import TemporaryDirectory
import unittest

from premisedb import RecordStore
from premisedb.cli import main


class FeatureDefinitionTests(unittest.TestCase):
    """A feature definition holds the instructions and type its values follow, outside the graph's records;
    values are typed against it, and its instructions are fixed once values name it."""

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.db = RecordStore(self.path)
        self.addCleanup(self.db.close)
        self.market = self.db.create("Entity", text="US regional banks", author_id="setup")
        self.funding = self.db.define_feature(author_id="researcher", key="flighty_funding", type="number", min=0, max=1,
                                              instructions="How much of the subject's funding could leave within weeks, "
                                                           "from 0 (none) to 1 (all).")
        self.dividend = self.db.define_feature(author_id="researcher", key="dividend", type="category",
                                               categories=["raised", "kept", "cut", "suspended"],
                                               instructions="How the latest dividend decision is described.")

    def value(self, definition, value, *, store=None, **attributes):
        store = store or self.db
        return store.contribute(author_id="agent", records=[{
            "ref": "v", "kind": "Variable", "text": definition.key,
            "attributes": {"key": definition.key, "feature": definition.id, "value": value} | attributes}],
            links=[{"from_id": "v", "to_id": self.market.id, "relation": "about"}]).records["v"]

    def test_a_definition_needs_instructions_and_a_type(self):
        for fields, message in (
                ({"key": " ", "type": "number"}, "needs key"),
                ({"key": "x", "type": "text"}, "type is one of"),
                ({"key": "x", "type": "category"}, "exactly when its type is category"),
                ({"key": "x", "type": "number", "categories": ["a"]}, "exactly when its type is category"),
                ({"key": "x", "type": "category", "categories": ["a", "a"]}, "distinct"),
                ({"key": "x", "type": "boolean", "min": 0}, "type number or count"),
                ({"key": "x", "type": "number", "min": 2, "max": 1}, "not be above"),
                ({"key": "x", "type": "number", "formula": "a * 2 +"}, "formula syntax"),
                ({"key": "x", "type": "number", "formula": "log(a)"}, "formula calls must be")):
            with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, message):
                self.db.define_feature(author_id="r", instructions="Instructions.", **fields)
        self.assertEqual(self.db.list_records(kind="Variable").records, ())  # Definitions are not records.
        self.assertEqual([f.key for f in self.db.list_features()], ["dividend", "flighty_funding"])
        self.assertEqual(self.db.read_feature("dividend"), self.dividend)
        self.assertEqual(self.db.read_feature(self.dividend.id[:8]).categories, ("raised", "kept", "cut", "suspended"))
        with self.assertRaises(KeyError):
            self.db.read_feature("nothing")

    def test_values_are_typed_against_their_feature(self):
        self.assertEqual(self.value(self.funding, 0.4).attributes["value"], 0.4)
        self.value(self.dividend, "cut")
        self.value(self.funding, [0.2, None], periods=["2022", "2023"])
        self.value(self.funding, None)
        count = self.db.define_feature(author_id="r", key="new_risks", type="count", instructions="New risk factors.")
        flag = self.db.define_feature(author_id="r", key="doubt", type="boolean", instructions="Going-concern doubt.")
        self.value(count, 3)
        self.value(flag, False)
        for feature, value, message in ((self.funding, 1.5, "a number from 0 to 1"), (self.funding, "high", "a number"),
                                        (self.dividend, "halved", "one of 'raised'"), (count, -1, "a whole number"),
                                        (count, 2.5, "a whole number"), (flag, 1, "true or false"),
                                        (self.funding, [0.2, 2], "a number from 0 to 1")):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, message):
                self.value(feature, value)
        with self.assertRaisesRegex(ValueError, "carries key 'flighty_funding'"):
            self.value(self.funding, 0.4, key="other")
        with self.assertRaisesRegex(ValueError, "carries its formula"):
            self.value(self.funding, 0.4, formula="1 - 0.6")
        with self.assertRaisesRegex(ValueError, "full ID"):
            self.value(self.funding, 0.4, feature=self.funding.id[:8])
        with self.assertRaisesRegex(ValueError, "only a Variable"):
            self.db.create("Finding", text="A finding.", author_id="agent", attributes={"feature": self.funding.id})
        self.assertEqual(self.db.read_feature(self.funding.id).values, 3)

    def test_edits_and_versions_of_a_value_stay_within_its_feature(self):
        empty = self.value(self.funding, None)
        with self.assertRaisesRegex(ValueError, "not a value of feature"):
            self.db.patch(empty.id, author_id="agent", attributes={"value": 7})  # In place: there was no value.
        first = self.db.patch(empty.id, author_id="agent", attributes={"value": 0.3})
        with self.assertRaisesRegex(ValueError, "not a value of feature"):
            self.db.patch(first.id, author_id="agent", attributes={"value": 7})  # A new version.
        version = self.db.patch(first.id, author_id="agent", attributes={"value": 0.5})
        self.assertEqual(version.attributes["feature"], self.funding.id)
        for change in ({"attributes": {"feature": self.dividend.id, "key": "dividend", "value": "cut"}},
                       {"remove": ["feature"]}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "stays fixed"):
                self.db.patch(version.id, author_id="agent", **change)

    def test_a_definition_is_fixed_once_values_name_it(self):
        edited = self.db.update_feature(self.funding.id, author_id="researcher", type="number", min=0, max=1,
                                        instructions="Reworded before any value.")
        self.assertEqual((edited.instructions, edited.id, edited.key), ("Reworded before any value.", self.funding.id, "flighty_funding"))
        self.value(self.funding, 0.4)
        with self.assertRaisesRegex(ValueError, "its definition is fixed"):
            self.db.update_feature(self.funding.id, author_id="researcher", type="number", instructions="Other.")
        with self.assertRaisesRegex(ValueError, "already defined"):
            self.db.define_feature(author_id="researcher", key="flighty_funding", type="number", instructions="Other.")
        with self.assertRaisesRegex(ValueError, "current definition"):
            self.db.define_feature(author_id="researcher", key="flighty_funding", type="number", instructions="Other.",
                                   replaces=self.dividend.id)
        newer = self.db.define_feature(author_id="researcher", key="flighty_funding", type="number", min=0, max=1,
                                       instructions="Other.", replaces=self.funding.id)
        self.assertEqual(self.db.read_feature("flighty_funding").id, newer.id)
        self.assertEqual(self.db.read_feature(self.funding.id).replaced_by, newer.id)
        self.assertEqual([f.id for f in self.db.list_features(replaced=True) if f.key == "flighty_funding"],
                         [self.funding.id, newer.id])
        self.value(self.funding, 0.5)  # Values of the replaced definition are still typed against it.

    def test_definitions_are_read_at_every_cutoff_and_written_without_one(self):
        then = RecordStore(self.path, as_of="2022-11-07")
        self.addCleanup(then.close)
        self.assertEqual(then.read_feature("flighty_funding"), self.funding)
        with self.assertRaisesRegex(ValueError, "without a cutoff"):
            then.define_feature(author_id="r", key="x", type="boolean", instructions="Later idea.")
        share = self.db.define_feature(author_id="researcher", key="uninsured_share", type="number", max=1,
                                       formula="uninsured / total", instructions="Uninsured over total deposits.")
        inputs = [then.contribute(author_id="agent", records=[{"ref": "v", "kind": "Variable", "text": key,
                  "attributes": {"key": key, "value": value}}], links=[{"from_id": "v", "to_id": self.market.id,
                  "relation": "about"}]).records["v"] for key, value in (("uninsured", 150), ("total", 100))]
        derived = then.contribute(author_id="agent", records=[{"ref": "v", "kind": "Variable", "text": "share", "attributes": {
            "key": "uninsured_share", "feature": share.id, "value": None, "formula": "uninsured / total"}}], links=[
            {"from_id": "v", "to_id": self.market.id, "relation": "about"}]
            + [{"from_id": "v", "to_id": v.id, "relation": "depends_on"} for v in inputs]).records["v"]
        with self.assertRaisesRegex(ValueError, "not a value of feature uninsured_share"):
            then.fill_model(derived.id, author_id="agent")  # 1.5 is above the feature's max.
        self.assertIsNone(then.read(derived.id).attributes["value"])

    def test_cli_defines_lists_and_reads_features(self):
        self.db.close()

        def run(*args, payload=None):
            import contextlib, io, sys
            out = io.StringIO()
            stdin = sys.stdin
            sys.stdin = io.StringIO(json.dumps(payload) if payload is not None else "")
            try:
                with contextlib.redirect_stdout(out):
                    self.assertEqual(main(["--db", str(self.path), *args]), 0)
            finally:
                sys.stdin = stdin
            return json.loads(out.getvalue())

        made = run("define-feature", "--input", "-", payload={"author_id": "r", "key": "doubt", "type": "boolean",
                                                               "instructions": "Whether the auditor states going-concern doubt."})
        self.assertEqual(run("feature", "doubt")["id"], made["id"])
        self.assertEqual([f["key"] for f in run("features")["features"]], ["dividend", "doubt", "flighty_funding"])
        self.assertEqual(run("update-feature", made["id"][:8], "--input", "-", payload={
            "author_id": "r", "type": "boolean", "instructions": "Reworded."})["instructions"], "Reworded.")


if __name__ == "__main__":
    unittest.main()
