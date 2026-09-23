from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from premisedb import RecordStore, SourceSegment


class DocumentSearchTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.db = RecordStore(self.path)
        self.addCleanup(self.db.close)

    def source(self, parts, description="Filing"):
        source = self.db.ingest_source(description=description, document_text="".join(parts),
                                       author_id="alice")
        ranges = []
        start = 0
        for part in parts:
            end = start + len(part.encode("utf-8"))
            ranges.append((start, end))
            start = end
        self.db.set_passages(source.id, ranges)
        return source

    def test_passage_ranking_ties_and_separate_record_search(self):
        parts = ["Utility capacity.\n\n", "Utility approval.\n\n", "Utility approval.\n\n"]
        source = self.source(parts, description="Descriptiononly")
        tied = self.source([parts[1]])
        self.source(["Copper exports."])
        self.db.create("Finding", text="Claimonly utility", author_id="alice")
        hits = self.db.search_documents("utility approval")
        self.assertEqual([hit.text for hit in hits], [parts[1], parts[2], parts[1], parts[0]])
        self.assertEqual([hit.source_id for hit in hits], [source.id, source.id, tied.id, source.id])
        self.assertLess(hits[0].start_byte, hits[1].start_byte)
        self.assertEqual(self.db.search_documents("utility approval", limit=1), hits[:1])
        self.assertEqual(self.db.search_documents("descriptiononly claimonly absent"), ())
        self.assertEqual(self.db.search_records("approval"), ())
        self.assertEqual(self.db.search_records("descriptiononly"), (source,))

    def test_source_filter_precedes_limit_and_empty_filter_matches_nothing(self):
        self.source(["Utility approval."])
        included = self.source(["Utility capacity."])
        other = self.source(["Approval pending."])
        hits = self.db.search_documents("utility approval", source_ids=[included.id], limit=1)
        self.assertEqual([hit.source_id for hit in hits], [included.id])
        self.assertEqual(self.db.search_documents("utility", source_ids=[included.id] * 2), hits)
        ids = (source_id for source_id in (included.id, other.id))
        self.assertEqual({hit.source_id for hit in self.db.search_documents(
            "utility approval", source_ids=ids)}, {included.id, other.id})
        self.assertEqual(self.db.search_documents("utility", source_ids=[]), ())
        unchunked = self.db.ingest_source(description="Pending", document_text="utility",
                                         author_id="alice")
        self.assertEqual(self.db.search_documents("utility", source_ids=[unchunked.id]), ())

    def test_exact_unicode_ranges_and_plain_text_queries(self):
        prefix, body = "€🙂 Overview.\r\n\r\n", "Cafe\u0301 debt\t€50\r\n"
        source = self.source([prefix, body])
        expected = SourceSegment(source.id, len(prefix.encode()), len((prefix + body).encode()), body)
        self.assertEqual(self.db.search_documents('"CAFE\u0301", debt?'), (expected,))
        self.assertEqual(self.db.read_source_text(source.id, start_byte=expected.start_byte,
                                                  end_byte=expected.end_byte), body)
        literal = self.source(["AND"])
        self.assertEqual(self.db.search_documents("AND")[0].source_id, literal.id)
        for query in ("", " \n ", '() : * "'):
            with self.subTest(query=query):
                self.assertEqual(self.db.search_documents(query), ())

    def test_rechunking_replaces_hits_across_connections_and_reopening(self):
        source = self.source(["Utility approval.\n\nDebt maturity."])
        other = self.source(["Debt covenants."])
        original = self.db.search_documents("utility")
        other_hits = self.db.search_documents("debt", source_ids=[other.id])
        with RecordStore(self.path) as reader:
            self.assertEqual(reader.search_documents("utility"), original)
            self.db.chunk_source(source.id, max_chars=20)
            updated = reader.search_documents("utility")
            self.assertEqual([hit.text for hit in updated], ["Utility approval.\n\n"])
            self.assertEqual(reader.search_documents("debt", source_ids=[other.id]), other_hits)
        self.db.close()
        with RecordStore(self.path) as reopened:
            self.assertEqual(reopened.search_documents("utility"), updated)
            self.assertEqual(reopened.read(source.id), source)

    def test_failed_rechunking_rolls_back_index_deletes_and_partial_inserts(self):
        source = self.source(["Utility approval.\n\nDebt maturity."])
        original = self.db.search_documents("utility debt")
        with closing(sqlite3.connect(self.path, isolation_level=None)) as sql:
            sql.execute("""CREATE TRIGGER reject_passage AFTER INSERT ON passages
                WHEN new.ordinal = 1 BEGIN SELECT RAISE(ABORT, 'injected failure'); END;""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected failure"):
            self.db.chunk_source(source.id, max_chars=20)
        self.assertEqual(self.db.search_documents("utility debt"), original)
        with RecordStore(self.path) as reader:
            self.assertEqual(reader.search_documents("utility debt"), original)

    def test_selected_ranges_remain_valid_when_passages_change_before_text_read(self):
        source = self.source(["Utility approval.\n\nDebt maturity."])
        original = self.db.search_documents("utility")
        read_text = self.db.read_source_text
        with RecordStore(self.path) as writer:
            def rechunk_then_read(*args, **kwargs):
                writer.chunk_source(source.id, max_chars=20)
                return read_text(*args, **kwargs)

            with patch.object(self.db, "read_source_text", side_effect=rechunk_then_read):
                self.assertEqual(self.db.search_documents("utility"), original)
        self.assertNotEqual(self.db.search_documents("utility"), original)

    def test_invalid_arguments_and_missing_source_content(self):
        for query in (None, 42):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.db.search_documents(query)
        for options in ({"limit": 0}, {"limit": 1001}, {"limit": True}, {"limit": 1.5},
                        {"source_ids": "id"}, {"source_ids": b"id"}, {"source_ids": 42},
                        {"source_ids": [None]}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.db.search_documents("utility", **options)
        finding = self.db.create("Finding", text="utility", author_id="alice")
        empty_source = self.db.create("Source", text="utility", author_id="alice")
        for source_id in ("missing", finding.id, empty_source.id):
            with self.subTest(source_id=source_id), self.assertRaises(KeyError):
                self.db.search_documents("utility", source_ids=[source_id])
