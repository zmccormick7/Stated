from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from premisedb import RecordStore


class PatchTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.db = RecordStore(self.path)
        self.addCleanup(self.db.close)
        self.record = self.db.create("Variable", text="Growth 35%", author_id="alice",
                                     attributes={"key": "growth", "value": 0.35, "low": 0.1, "status": "draft"})

    def test_patch_changes_only_named_attributes_and_text(self):
        before = datetime.now(UTC)
        patched = self.db.patch(self.record.id, author_id="bob",
                                attributes={"value": 0.45, "periods": ["2027", "2028"]}, remove=["status"])
        self.assertEqual(patched.attributes, {"key": "growth", "value": 0.45, "low": 0.1, "periods": ["2027", "2028"]})
        self.assertEqual((patched.text, patched.author_id), ("Growth 35%", "bob"))
        self.assertLessEqual(before, datetime.fromisoformat(patched.updated_at))
        renamed = self.db.patch(self.record.id, author_id="carol", text="Growth 45%", remove=["absent"])
        self.assertEqual((renamed.text, renamed.attributes), ("Growth 45%", patched.attributes))
        self.assertEqual(self.db.read(self.record.id), renamed)

    def test_patches_from_separate_stores_keep_each_others_changes(self):
        with RecordStore(self.path) as other:
            self.db.patch(self.record.id, author_id="bob", attributes={"value": 0.45})
            other.patch(self.record.id, author_id="carol", attributes={"reviewed": "Reviewed; value unchanged."})
        self.assertEqual(self.db.read(self.record.id).attributes,
                         {"key": "growth", "value": 0.45, "low": 0.1, "status": "draft",
                          "reviewed": "Reviewed; value unchanged."})

    def test_patch_citations_replace_the_attribute_and_link_the_source(self):
        source = self.db.ingest_source(description="Filing", document_text="Demand exceeds supply.", author_id="a")
        patched = self.db.patch(self.record.id, author_id="bob", citations=[{"source_id": source.id, "quote": "Demand"}])
        self.assertEqual(patched.attributes["citations"],
                         [{"source_id": source.id, "start_byte": 0, "end_byte": 6, "quote": "Demand"}])
        self.assertEqual(patched.attributes["value"], 0.35)
        self.assertEqual([l.to_id for l in self.db.connections(self.record.id, relation="supported_by").links],
                         [source.id])

    def test_invalid_patches_change_nothing(self):
        for kwargs, error in (({}, ValueError), ({"attributes": []}, ValueError), ({"remove": "status"}, ValueError),
                              ({"remove": [1]}, ValueError), ({"attributes": {"value": 1}, "remove": ["value"]}, ValueError),
                              ({"attributes": {"value": float("nan")}}, ValueError),
                              ({"attributes": {"citations": []}, "citations": []}, ValueError),
                              ({"citations": [{"source_id": "missing", "quote": "x"}]}, KeyError)):
            with self.subTest(kwargs=kwargs), self.assertRaises(error):
                self.db.patch(self.record.id, author_id="bob", **kwargs)
        with self.assertRaises(KeyError):
            self.db.patch("missing", author_id="bob", text="x")
        self.assertEqual(self.db.read(self.record.id), self.record)


if __name__ == "__main__":
    unittest.main()
