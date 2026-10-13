from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from premisedb import RecordStore


class VariableVersionTests(unittest.TestCase):
    """A change to a Variable's value is a new version; the old one keeps its value and its date."""

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.db = RecordStore(self.path)
        self.addCleanup(self.db.close)
        self.model = self.db.create("Model", text="NVIDIA model", author_id="a")
        self.source = self.db.ingest_source(description="Call", document_text="Growth of 35% is expected.",
                                            author_id="a", attributes={"published": "2026-09-01"})
        self.evidence = self.db.create("Finding", text="Growth of 35%.", author_id="a", attributes={"basis": "disclosed"},
                                       citations=[{"source_id": self.source.id, "quote": "Growth of 35%"}])
        self.growth = self.db.create("Variable", text="Growth", author_id="a",
                                     attributes={"key": "growth", "role": "input", "value": 0.35, "units": "ratio"})
        self.db.create_link(self.growth.id, self.model.id, "in_model", author_id="a")
        self.db.create_link(self.growth.id, self.evidence.id, "depends_on", author_id="a")
        self.revenue = self.db.create("Variable", text="Revenue", author_id="a",
                                      attributes={"key": "revenue", "role": "output", "value": None, "formula": "100 * (1 + growth)"})
        self.db.create_link(self.revenue.id, self.model.id, "in_model", author_id="a")
        self.db.create_link(self.revenue.id, self.growth.id, "depends_on", author_id="a")

    def versions(self, key):
        return [r for r in self.db.list_records(kind="Variable", where={"key": key}).records]

    def test_a_value_change_is_a_new_version_carrying_the_links(self):
        version = self.db.patch(self.growth.id, author_id="b", attributes={"value": 0.45, "basis": "inference"})
        self.assertNotEqual(version.id, self.growth.id)
        self.assertEqual((version.kind, version.text, version.author_id, version.created_by), ("Variable", "Growth", "b", "b"))
        self.assertEqual(version.attributes, {"key": "growth", "role": "input", "value": 0.45, "units": "ratio", "basis": "inference"})
        self.assertEqual(self.db.read(self.growth.id), self.growth)  # Untouched, value and date.
        links = {(l.relation, l.to_id) for l in self.db.connections(version.id, direction="out").links}
        self.assertEqual(links, {("updates", self.growth.id), ("in_model", self.model.id), ("depends_on", self.evidence.id)})
        self.assertEqual([v.attributes["value"] for v in self.versions("growth")], [0.35, 0.45])
        # The model reads the latest version; a second change versions again, from the latest.
        self.assertEqual({c.key: c.computed for c in self.db.check_model(self.model.id).checked}, {"revenue": 145.0})
        third = self.db.update(version.id, text="Growth, revised", author_id="c",
                               attributes={"key": "growth", "role": "input", "value": 0.4},
                               citations=[{"source_id": self.source.id, "quote": "expected"}])
        self.assertEqual((third.text, third.attributes["value"], third.attributes["citations"][0]["quote"]), ("Growth, revised", 0.4, "expected"))
        self.assertEqual({l.relation for l in self.db.connections(third.id, direction="out").links},
                         {"updates", "in_model", "depends_on", "supported_by"})
        self.assertEqual(self.db.read_link(self.db.connections(third.id, direction="out", relation="updates").links[0].id).to_id, version.id)

    def test_edits_that_do_not_change_a_value_stay_in_place(self):
        same = self.db.patch(self.growth.id, author_id="b", attributes={"value": 0.35, "units": "fraction"})
        self.assertEqual((same.id, same.attributes["units"]), (self.growth.id, "fraction"))
        text = self.db.patch(self.growth.id, author_id="b", text="Growth rate")
        self.assertEqual((text.id, text.text), (self.growth.id, "Growth rate"))
        first = self.db.patch(self.revenue.id, author_id="b", attributes={"value": 135.0})  # No value yet: filled in place.
        self.assertEqual((first.id, first.attributes["value"]), (self.revenue.id, 135.0))
        self.assertEqual(len(self.versions("growth")), 1)
        self.assertEqual(len(self.versions("revenue")), 1)

    def test_fill_model_versions_an_output_whose_inputs_changed(self):
        self.assertEqual([(w.key, w.id) for w in self.db.fill_model(self.model.id, author_id="a").written],
                         [("revenue", self.revenue.id)])  # The first value goes in place.
        growth = self.db.patch(self.growth.id, author_id="b", attributes={"value": 0.45})
        fill = self.db.fill_model(self.model.id, author_id="a")
        self.assertEqual([(w.key, w.stored, w.value) for w in fill.written], [("revenue", 135.0, 145.0)])
        version = self.db.read(fill.written[0].id)
        self.assertNotEqual(version.id, self.revenue.id)
        self.assertEqual({(l.relation, l.to_id) for l in self.db.connections(version.id, direction="out").links},
                         {("updates", self.revenue.id), ("in_model", self.model.id), ("depends_on", growth.id)})
        self.assertEqual(self.db.read(self.revenue.id).attributes["value"], 135.0)
        self.assertTrue(all(c.exact for c in self.db.check_model(self.model.id).checked))

    def test_a_version_may_follow_a_value_written_at_a_cutoff(self):
        then = RecordStore(self.path, as_of="2026-09-01")
        self.addCleanup(then.close)
        old = then.create("Variable", text="Margin", author_id="a", attributes={"key": "margin", "value": 0.6})
        with self.assertRaisesRegex(ValueError, "cannot be edited"):
            self.db.patch(old.id, author_id="b", text="Gross margin")  # Not a value change: refused as before.
        version = self.db.patch(old.id, author_id="b", attributes={"value": 0.62})
        self.assertEqual((version.as_of == old.as_of, version.attributes["value"], then.read(old.id).attributes["value"]),
                         (False, 0.62, 0.6))
        self.assertEqual([v.attributes["value"] for v in then.list_records(kind="Variable", where={"key": "margin"}).records], [0.6])
