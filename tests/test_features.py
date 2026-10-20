from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from premisedb import RecordStore


class FeatureDefinitionTests(unittest.TestCase):
    """A Feature holds the instructions and type its values follow; values are typed against it, and the
    instructions are fixed once values rest on them."""

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.db = RecordStore(self.path)
        self.addCleanup(self.db.close)
        self.market = self.db.create("Entity", text="US regional banks", author_id="setup")
        self.reliance = self.db.create("Feature", author_id="researcher", text="How much the subject relies on deposits "
                                       "that could leave quickly, from 0 (none) to 1 (all).",
                                       attributes={"key": "flighty_funding", "type": "number", "min": 0, "max": 1})
        self.dividend = self.db.create("Feature", author_id="researcher", text="How the latest dividend decision is described.",
                                       attributes={"key": "dividend", "type": "category",
                                                   "categories": ["raised", "kept", "cut", "suspended"]})

    def value(self, feature, value, *, store=None, key=None, **attributes):
        store = store or self.db
        result = store.contribute(author_id="agent", records=[{
            "ref": "v", "kind": "Variable", "text": feature.attributes["key"],
            "attributes": {"key": key or feature.attributes["key"], "value": value} | attributes}],
            links=[{"from_id": "v", "to_id": self.market.id, "relation": "about"},
                   {"from_id": "v", "to_id": feature.id, "relation": "defined_by"}])
        return result.records["v"]

    def test_a_definition_needs_a_key_and_a_type(self):
        for attributes, message in (
                ({"type": "number"}, "needs attributes.key"),
                ({"key": "x", "type": "text"}, "needs attributes.type"),
                ({"key": "x", "type": "category"}, "exactly when its type is category"),
                ({"key": "x", "type": "number", "categories": ["a"]}, "exactly when its type is category"),
                ({"key": "x", "type": "category", "categories": ["a", "a"]}, "distinct"),
                ({"key": "x", "type": "boolean", "min": 0}, "type number or count"),
                ({"key": "x", "type": "number", "min": 2, "max": 1}, "not be above"),
                ({"key": "x", "type": "number", "formula": 3}, "formula must be a string")):
            with self.subTest(attributes=attributes), self.assertRaisesRegex(ValueError, message):
                self.db.create("Feature", text="Instructions.", author_id="researcher", attributes=attributes)
        with self.assertRaisesRegex(ValueError, "needs attributes.type"):
            self.db.patch(self.reliance.id, author_id="researcher", remove=["type"])

    def test_values_are_typed_against_their_feature(self):
        self.assertEqual(self.value(self.reliance, 0.4).attributes["value"], 0.4)
        self.value(self.dividend, "cut")
        self.value(self.reliance, [0.2, None], periods=["2022", "2023"])
        self.value(self.reliance, None)
        count = self.db.create("Feature", text="New risk factors.", author_id="r", attributes={"key": "new_risks", "type": "count"})
        flag = self.db.create("Feature", text="Going-concern doubt.", author_id="r", attributes={"key": "doubt", "type": "boolean"})
        self.value(count, 3)
        self.value(flag, False)
        for feature, value, message in ((self.reliance, 1.5, "a number from 0 to 1"), (self.reliance, "high", "a number"),
                                        (self.dividend, "halved", "one of 'raised'"), (count, -1, "a whole number"),
                                        (count, 2.5, "a whole number"), (flag, 1, "true or false"),
                                        (self.reliance, [0.2, 2], "a number from 0 to 1")):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, message):
                self.value(feature, value)
        with self.assertRaisesRegex(ValueError, "carries key 'flighty_funding'"):
            self.value(self.reliance, 0.4, key="other")
        with self.assertRaisesRegex(ValueError, "carries its formula"):
            self.value(self.reliance, 0.4, formula="1 - 0.6")
        finding = self.db.create("Finding", text="A finding.", author_id="agent")
        with self.assertRaisesRegex(ValueError, "links a Variable to the Feature"):
            self.db.create_link(finding.id, self.reliance.id, "defined_by", author_id="agent")
        with self.assertRaisesRegex(ValueError, "already a value of Feature"):
            self.db.create_link(self.value(self.reliance, 0.4).id, self.dividend.id, "defined_by", author_id="agent")

    def test_edits_and_versions_of_a_value_stay_within_its_feature(self):
        empty = self.value(self.reliance, None)
        with self.assertRaisesRegex(ValueError, "not a value of Feature"):
            self.db.patch(empty.id, author_id="agent", attributes={"value": 7})  # In place: there was no value.
        first = self.db.patch(empty.id, author_id="agent", attributes={"value": 0.3})
        with self.assertRaisesRegex(ValueError, "not a value of Feature"):
            self.db.patch(first.id, author_id="agent", attributes={"value": 7})  # A new version.
        version = self.db.patch(first.id, author_id="agent", attributes={"value": 0.5})
        links = {(l.relation, l.to_id) for l in self.db.connections(version.id, direction="out").links}
        self.assertIn(("defined_by", self.reliance.id), links)
        link = next(l for l in self.db.connections(version.id, direction="out").links if l.relation == "defined_by")
        self.assertEqual(self.db.patch_link(link.id, author_id="agent", explanation="Made under it.").explanation, "Made under it.")
        for change in ({"to_id": self.dividend.id}, {"relation": "about"}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "changes only its explanation"):
                self.db.patch_link(link.id, author_id="agent", **change)
        about = next(l for l in self.db.connections(version.id, direction="out").links if l.relation == "about")
        with self.assertRaisesRegex(ValueError, "changes only its explanation"):
            self.db.patch_link(about.id, author_id="agent", relation="defined_by")

    def test_a_feature_with_values_keeps_its_instructions(self):
        self.assertEqual(self.db.patch(self.reliance.id, author_id="researcher", attributes={"max": 1.0}).attributes["max"], 1.0)
        self.value(self.reliance, 0.4)
        for change in ({"text": "Other instructions."}, {"attributes": {"max": 2}}, {"remove": ["min"]}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "write the new ones as a new Feature"):
                self.db.patch(self.reliance.id, author_id="researcher", **change)
        with self.assertRaisesRegex(ValueError, "write the new ones as a new Feature"):
            self.db.update(self.reliance.id, author_id="researcher", text="Other.", attributes=self.reliance.attributes)
        noted = self.db.patch(self.reliance.id, author_id="researcher", attributes={"note": "First used 2026-10-19."})
        self.assertEqual(noted.attributes["note"], "First used 2026-10-19.")

    def test_a_feature_is_seen_at_every_cutoff_and_derived_values_stay_typed(self):
        then = RecordStore(self.path, as_of="2022-11-07")
        self.addCleanup(then.close)
        self.assertEqual(then.read(self.reliance.id).as_of, self.reliance.as_of)  # Written today, seen in 2022.
        self.assertEqual([r.id for r in then.list_records(kind="Feature").records], [self.reliance.id, self.dividend.id])
        share = self.db.create("Feature", text="Uninsured over total deposits.", author_id="researcher",
                               attributes={"key": "uninsured_share", "type": "number", "max": 1,
                                           "formula": "uninsured / total"})
        inputs = {key: then.contribute(author_id="agent", records=[{"ref": "v", "kind": "Variable", "text": key,
                  "attributes": {"key": key, "value": value}}], links=[{"from_id": "v", "to_id": self.market.id,
                  "relation": "about"}]).records["v"] for key, value in (("uninsured", 150), ("total", 100))}
        derived = then.contribute(author_id="agent", records=[{"ref": "v", "kind": "Variable", "text": "share", "attributes": {
            "key": "uninsured_share", "value": None, "formula": "uninsured / total"}}], links=[
            {"from_id": "v", "to_id": share.id, "relation": "defined_by"},
            {"from_id": "v", "to_id": self.market.id, "relation": "about"}]
            + [{"from_id": "v", "to_id": v.id, "relation": "depends_on"} for v in inputs.values()]).records["v"]
        with self.assertRaisesRegex(ValueError, "not a value of Feature uninsured_share"):
            then.fill_model(derived.id, author_id="agent")  # 1.5 is above the Feature's max.
        self.assertIsNone(then.read(derived.id).attributes["value"])


if __name__ == "__main__":
    unittest.main()
