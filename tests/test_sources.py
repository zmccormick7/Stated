from contextlib import closing
from hashlib import sha256
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from premisedb import RecordStore


class SourceTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.db = RecordStore(self.path)
        self.addCleanup(self.db.close)

    def ingest(self, text="Energization delayed."):
        return self.db.ingest_source(description="Supplier filing", document_text=text,
                                     author_id="alice", attributes={"origin": "filing.txt"})

    def test_ingestion_separates_description_from_exact_text_and_survives_reopening(self):
        text = "  Cafe\u0301\r\nEnergization costs €5.\n"
        source = self.ingest(text)
        self.assertEqual((source.kind, source.text, source.attributes),
                         ("Source", "Supplier filing", {"origin": "filing.txt"}))
        self.assertEqual(self.db.search_records("supplier", kind="Source"), (source,))
        self.assertEqual(self.db.search_records("energization"), ())
        with RecordStore(self.path) as other:
            self.assertEqual(other.read_source_text(source.id), text)
        with closing(sqlite3.connect(self.path)) as sql:
            body, digest = sql.execute("SELECT text_utf8, text_sha256 FROM source_content").fetchone()
            self.assertEqual(body, text.encode("utf-8"))
            self.assertEqual(digest, sha256(body).hexdigest())
        self.db.close()
        with RecordStore(self.path) as reopened:
            self.assertEqual(reopened.read_source_text(source.id), text)

    def test_ranges_use_bytes_and_require_character_boundaries(self):
        source = self.ingest("A€🙂Z")  # UTF-8 boundaries: 0, 1, 4, 8, 9.
        self.assertEqual(self.db.read_source_text(source.id, start_byte=1, end_byte=8), "€🙂")
        self.assertEqual(self.db.read_source_text(source.id, start_byte=8), "Z")
        self.assertEqual(self.db.read_source_text(source.id, start_byte=9), "")
        for start, end in ((-1, 1), (3, 1), (0, 10), (2, 8), (1, 5), (2, 2), (True, 4), (0, 1.5)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.db.read_source_text(source.id, start_byte=start, end_byte=end)

    def test_locate_returns_exact_byte_ranges_without_normalization(self):
        text = "A€ ramp.\r\nThe €5 ramp’s risk; the €5 ramp's risk. €5€5€5"
        source = self.ingest(text)
        matches = self.db.locate_source_text(source.id, "€5 ramp")
        self.assertEqual([(m.start_byte, m.end_byte) for m in matches], [(16, 25), (40, 49)])
        for match in matches:
            self.assertEqual((match.source_id, match.text, match.document_context), (source.id, "€5 ramp", None))
            self.assertEqual(self.db.read_source_text(source.id, start_byte=match.start_byte,
                                                      end_byte=match.end_byte), "€5 ramp")
        self.assertEqual(len(self.db.locate_source_text(source.id, "ramp's")), 1)  # No quote folding.
        self.assertEqual(self.db.locate_source_text(source.id, "ramp.\n"), ())  # Line endings are exact.
        self.assertEqual(len(self.db.locate_source_text(source.id, "€5€5")), 1)  # Nonoverlapping.
        self.assertEqual(len(self.db.locate_source_text(source.id, "€5", limit=2)), 2)
        for quote, limit in (("", 10), (" \n", 10), (None, 10), ("\ud800", 10), ("ramp", 0), ("ramp", True)):
            with self.subTest(quote=quote, limit=limit), self.assertRaises(ValueError):
                self.db.locate_source_text(source.id, quote, limit=limit)
        with self.assertRaises(KeyError):
            self.db.locate_source_text("missing", "ramp")

    def test_record_edits_leave_canonical_text_fixed(self):
        source = self.ingest()
        self.db.update(source.id, text="Corrected description", author_id="bob")
        self.assertEqual(self.db.read_source_text(source.id), "Energization delayed.")
        with closing(sqlite3.connect(self.path)) as sql:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "source text is fixed"):
                sql.execute("UPDATE source_content SET text_utf8 = ?", (b"Changed",))
        another = self.ingest("Energization approved.")
        self.assertNotEqual(source.id, another.id)
        self.assertEqual(self.db.read_source_text(source.id), "Energization delayed.")
        self.assertEqual(self.db.read_source_text(another.id), "Energization approved.")

    def test_content_failure_rolls_back_source_record_and_search_index(self):
        with closing(sqlite3.connect(self.path, isolation_level=None)) as sql:
            sql.execute("""CREATE TRIGGER reject_content AFTER INSERT ON source_content BEGIN
                SELECT RAISE(ABORT, 'injected failure'); END;""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected failure"):
            self.ingest()
        self.assertEqual(self.db.search_records("supplier"), ())
        with closing(sqlite3.connect(self.path, isolation_level=None)) as sql:
            self.assertEqual(sql.execute("SELECT count(*) FROM records").fetchone()[0], 0)
            self.assertEqual(sql.execute("SELECT count(*) FROM source_content").fetchone()[0], 0)
            sql.execute("DROP TRIGGER reject_content")
        source = self.ingest()
        self.assertEqual(self.db.read_source_text(source.id), "Energization delayed.")

    def test_invalid_content_and_missing_ingestion(self):
        for text in ("", b"bytes", None, "\ud800"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.ingest(text)
        finding = self.db.create("Finding", text="A claim", author_id="alice")
        unprocessed = self.db.create("Source", text="A source reference", author_id="alice")
        for source_id in ("missing", finding.id, unprocessed.id):
            with self.subTest(source_id=source_id), self.assertRaises(KeyError):
                self.db.read_source_text(source_id)
        with closing(sqlite3.connect(self.path)) as sql:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "requires a Source"):
                sql.execute("""INSERT INTO source_content SELECT record_key, ?, ?
                    FROM records WHERE id = ?""", (b"text", sha256(b"text").hexdigest(), finding.id))
