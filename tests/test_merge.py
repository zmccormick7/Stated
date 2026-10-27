import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from stated import Merge, RecordStore


class MergeTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        with RecordStore(self.folder / "publisher.sqlite", layer="base") as db:
            self.base_vantage = db.create("Entity", text="Vantage Data Centers (Vantage)", author_id="base",
                                          attributes={"aliases": ["Vantage"]}).id
            db.export_layer(self.folder / "base.sqlite", version="2026.10.01")
        self.db_path = self.folder / "research.sqlite"
        with RecordStore(self.db_path, layer="fund") as db:
            db.attach_layer(self.folder / "base.sqlite")
            self.keep = db.create("Entity", text="Related Digital (Related Companies)", author_id="a",
                                  attributes={"aliases": ["Related Digital Infrastructure"]}).id
            self.dup = db.create("Entity", text="Related Digital, developer of the Frontier campus", author_id="b",
                                 attributes={"aliases": ["Related"]}).id
            self.finding = db.create("Finding", text="Related built the Frontier campus.", author_id="b").id
            self.question = db.create("Question", text="Who finances Related?", author_id="b").id
            db.create_link(self.finding, self.dup, "about", author_id="b")  # Moves.
            db.create_link(self.dup, self.question, "raises", author_id="b")  # Moves.
            db.create_link(self.finding, self.keep, "about", author_id="a")  # Already there: the duplicate's is dropped.
            self.pair = db.create_link(self.keep, self.dup, "relates_to", author_id="a").id  # Between the two: stays.

    def test_a_duplicate_is_folded_into_the_kept_record(self):
        with RecordStore(self.db_path) as db:
            result = db.merge(self.keep, self.dup, author_id="review")
            self.assertEqual(result, Merge(self.keep, self.dup, 1, 1))
            kept = db.read(self.keep)
            self.assertEqual(kept.attributes["aliases"], ["Related Digital Infrastructure", "Related", "Related Digital"])
            retired = db.read(self.dup)
            self.assertEqual((retired.attributes["status"], retired.text),
                             ("superseded", "Related Digital, developer of the Frontier campus"))
            self.assertIn(f"Duplicate of Entity {self.keep}", retired.attributes["status_reason"])
            links = {(l.from_id, l.to_id, l.relation) for l in db.connections(self.dup, limit=50).links}
            self.assertEqual(links, {(self.finding, self.dup, "about"), (self.keep, self.dup, "relates_to"),
                                     (self.keep, self.dup, "supersedes")})
            self.assertIn((self.keep, self.question, "raises"), {(l.from_id, l.to_id, l.relation)
                                                                  for l in db.connections(self.keep, limit=50).links})
            with self.assertRaisesRegex(ValueError, "already superseded"):
                db.merge(self.keep, self.dup, author_id="review")

    def test_the_kept_record_may_be_in_the_base_layer(self):
        with RecordStore(self.db_path) as db:
            dup = db.create("Entity", text="Vantage Data Centers (Vantage; private)", author_id="b").id
            db.create_link(self.finding, dup, "about", author_id="b")
            self.assertEqual(db.merge(self.base_vantage, dup, author_id="review"), Merge(self.base_vantage, dup, 1, 0))
            self.assertEqual(db.read(self.base_vantage).attributes["aliases"], ["Vantage"])  # Read-only: untouched.
            self.assertEqual(db.read(dup).attributes["status"], "superseded")
            self.assertEqual([l.relation for l in db.connections(self.base_vantage, limit=50).links], ["about", "supersedes"])

    def test_what_cannot_be_merged(self):
        with RecordStore(self.db_path) as db:
            source = db.ingest_source(description="A filing", document_text="text", author_id="a").id
            for keep, dup, message in ((self.keep, self.keep, "the same record"), (self.keep, self.finding, "same kind"),
                                       (source, source, "the same record"), (self.dup, self.base_vantage, "writable")):
                with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                    db.merge(keep, dup, author_id="review")
            with self.assertRaises(KeyError):
                db.merge(self.keep, "missing", author_id="review")

    def test_the_command(self):
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]), "OPENROUTER_API_KEY": ""}
        result = subprocess.run([sys.executable, "-m", "stated", "--db", str(self.db_path), "merge", self.keep[:8],
                                 self.dup[:8], "--input", "-"], input=json.dumps({"author_id": "review"}), text=True,
                                capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"keep_id": self.keep, "duplicate_id": self.dup, "moved": 1, "dropped": 1})


if __name__ == "__main__":
    unittest.main()
