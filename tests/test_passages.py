from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from premisedb import RecordStore
from premisedb.chunking import split_ranges


class ChunkingTests(unittest.TestCase):
    def test_boundary_preferences(self):
        for prefix in ("Intro words.\n\n", "Intro words.\r\n\r\n", "Intro words.\n"):
            with self.subTest(prefix=prefix):
                text = prefix + "More text follows after this."
                self.assertEqual(split_ranges(text, max_chars=24)[0], (0, len(prefix)))
        text = "Intro words. More text follows after this."
        self.assertEqual(split_ranges(text, max_chars=24)[0], (0, len("Intro words. More text ")))

    def test_lossless_partition_and_character_cap(self):
        for text in ("", "   \n\r\n\t", "x" * 103, "A€🙂 Cafe\u0301\r\n\r\n" * 13):
            for size in (1, 2, 7, 24, 2000):
                with self.subTest(text=text[:20], size=size):
                    body = text.encode("utf-8")
                    end = 0
                    pieces = []
                    for start, stop in split_ranges(text, max_chars=size):
                        self.assertEqual(start, end)
                        piece = body[start:stop].decode("utf-8")
                        self.assertTrue(0 < len(piece) <= size)
                        pieces.append(piece)
                        end = stop
                    self.assertEqual(end, len(body))
                    self.assertEqual("".join(pieces), text)

    def test_invalid_inputs(self):
        for size in (0, -1, True, 1.5):
            with self.subTest(size=size), self.assertRaises(ValueError):
                split_ranges("text", max_chars=size)
        with self.assertRaises(ValueError):
            split_ranges(b"bytes")


class PassageTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.db = RecordStore(self.path)
        self.addCleanup(self.db.close)
        self.source = self.db.ingest_source(description="Example", document_text="A€🙂Z",
                                            author_id="alice")

    def test_chunk_read_page_replace_and_reopen(self):
        self.assertEqual(self.db.read_passages(self.source.id), ())
        self.assertEqual(self.db.chunk_source(self.source.id, max_chars=1), 4)
        passages = self.db.read_passages(self.source.id)
        self.assertEqual([(p.start_byte, p.end_byte) for p in passages], [(0, 1), (1, 4), (4, 8), (8, 9)])
        self.assertEqual(self.db.read_passages(self.source.id, start_ordinal=1, limit=2), passages[1:3])
        self.assertEqual(self.db.read_passages(self.source.id, start_ordinal=4), ())
        other = self.db.ingest_source(description="Other", document_text="text", author_id="alice")
        self.db.chunk_source(other.id)
        self.assertEqual(self.db.set_passages(self.source.id, [(0, 4), (4, 9)]), 2)
        self.assertEqual(len(self.db.read_passages(other.id)), 1)
        self.db.close()
        with RecordStore(self.path) as reopened:
            self.assertEqual(len(reopened.read_passages(self.source.id)), 2)
            self.assertEqual(reopened.read_source_text(self.source.id), "A€🙂Z")
            self.assertEqual(reopened.read(self.source.id), self.source)

    def test_invalid_partitions_leave_existing_passages_intact(self):
        self.db.chunk_source(self.source.id)
        original = self.db.read_passages(self.source.id)
        for ranges in ([], [(0, 8)], [(0, 10)], [(1, 9)], [(0, 1), (4, 9)],
                       [(0, 4), (1, 9)], [(0, 2), (2, 9)], [(0, 0), (0, 9)], [(False, 9)]):
            with self.subTest(ranges=ranges), self.assertRaises(ValueError):
                self.db.set_passages(self.source.id, ranges)
            self.assertEqual(self.db.read_passages(self.source.id), original)

    def test_failed_replacement_rolls_back_deletion_and_partial_inserts(self):
        self.db.chunk_source(self.source.id)
        original = self.db.read_passages(self.source.id)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as sql:
            sql.execute("""CREATE TRIGGER reject_passage AFTER INSERT ON passages
                WHEN new.ordinal = 1 BEGIN SELECT RAISE(ABORT, 'injected failure'); END;""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected failure"):
            self.db.chunk_source(self.source.id, max_chars=1)
        self.assertEqual(self.db.read_passages(self.source.id), original)

    def test_missing_sources_and_invalid_read_limits(self):
        record = self.db.create("Source", text="No document yet", author_id="alice")
        for source_id in ("missing", record.id):
            with self.subTest(source_id=source_id):
                with self.assertRaises(KeyError):
                    self.db.chunk_source(source_id)
                with self.assertRaises(KeyError):
                    self.db.set_passages(source_id, [(0, 1)])
                with self.assertRaises(KeyError):
                    self.db.read_passages(source_id)
        for options in ({"start_ordinal": -1}, {"start_ordinal": True}, {"limit": 0}, {"limit": 1001}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.db.read_passages(self.source.id, **options)
