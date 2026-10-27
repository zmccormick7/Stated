from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from stated import DocumentContext, RecordStore


class DocumentContextTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.db = RecordStore(self.path)
        self.addCleanup(self.db.close)
        self.text = "€🙂 Overview.\r\n\r\n2028: $450m.\r\n"
        self.source = self.db.ingest_source(description="Metadataonly", document_text=self.text,
                                            author_id="alice")

    def context(self, source_id=None, **options):
        fields = dict(title="Acme FY2025", summary="Annual filing on operations and financial statements.",
                      author_id="agent")
        fields.update(options)
        return self.db.set_document_context(source_id or self.source.id, **fields)

    def test_context_before_chunking_is_separate_from_evidence_and_record_description(self):
        self.assertIsNone(self.db.read_document_context(self.source.id))
        context = self.context(provenance={"method": "agent-authored"})
        self.assertIsInstance(context, DocumentContext)
        self.assertEqual(self.db.search_documents("Acme"), ())
        self.db.chunk_source(self.source.id)
        hit, = self.db.search_documents("Acme FY2025")
        self.assertEqual(hit.document_context, context)
        self.assertEqual((hit.start_byte, hit.end_byte, hit.text), (0, len(self.text.encode()), self.text))
        self.assertEqual(context.header, context.title + "\n" + context.summary)
        self.assertEqual(self.db.search_records("Acme"), ())
        self.assertEqual(self.db.search_documents("Metadataonly"), ())
        context.provenance["method"] = "local mutation"
        self.assertEqual(self.db.read_document_context(self.source.id).provenance,
                         {"method": "agent-authored"})

    def test_updates_refresh_all_passages_and_survive_rechunking_and_reopening(self):
        split = len("€🙂 Overview.\r\n\r\n".encode())
        self.db.set_passages(self.source.id, [(0, split), (split, len(self.text.encode()))])
        original_passages = self.db.read_passages(self.source.id)
        self.context(title="Oldissuer", summary="Oldsummary", provenance={"method": "supplied"})
        self.assertEqual(len(self.db.search_documents("Oldissuer")), 2)
        other = self.db.ingest_source(description="Other", document_text="Debt.", author_id="bob")
        self.db.chunk_source(other.id)
        self.context(other.id, title="Oldissuer")
        other_hits = self.db.search_documents("Oldissuer", source_ids=[other.id])
        with RecordStore(self.path) as reader:
            before = datetime.now(UTC)
            updated = self.context(title="Newissuer", summary="Newsummary", author_id="bob")
            self.assertLessEqual(before, datetime.fromisoformat(updated.updated_at))
            self.assertLessEqual(datetime.fromisoformat(updated.updated_at), datetime.now(UTC))
            self.assertEqual((updated.author_id, updated.provenance), ("bob", {}))
            self.assertEqual(reader.search_documents("Oldissuer"), other_hits)
            self.assertEqual(reader.search_documents("Oldsummary"), ())
            self.assertEqual(len(reader.search_documents("Newsummary")), 2)
            self.assertEqual(reader.read_document_context(self.source.id), updated)
        self.assertEqual(self.db.read_passages(self.source.id), original_passages)
        self.assertEqual(self.db.read(self.source.id), self.source)
        self.db.chunk_source(self.source.id)
        hit, = self.db.search_documents("Newissuer")
        self.assertEqual((hit.text, hit.document_context), (self.text, updated))
        self.db.close()
        with RecordStore(self.path) as reopened:
            self.assertEqual(reopened.search_documents("Newissuer"), (hit,))
            self.assertEqual(reopened.read_source_text(self.source.id), self.text)

    def test_body_matches_get_more_weight_than_header_matches(self):
        header = self.db.ingest_source(description="First", document_text="Filler", author_id="alice")
        body = self.db.ingest_source(description="Second", document_text="Needle", author_id="alice")
        for source, title in ((header, "Needle"), (body, "Filler")):
            self.context(source.id, title=title, summary="")
            self.db.chunk_source(source.id)
        self.assertEqual([hit.source_id for hit in self.db.search_documents("Needle")], [body.id, header.id])
        self.assertEqual(self.db.search_documents("Needle", source_ids=[header.id], limit=1)[0].source_id,
                         header.id)

    def test_failed_context_insert_and_update_roll_back_index_changes(self):
        self.db.chunk_source(self.source.id)
        for existing in (None, self.context):
            if existing:
                existing()
            original = self.db.read_document_context(self.source.id)
            hits = self.db.search_documents("450m")
            with patch.object(self.db, "read_document_context", side_effect=RuntimeError("after indexing")):
                with self.assertRaisesRegex(RuntimeError, "after indexing"):
                    self.context(title="Rejected", summary="Failed")
            self.assertEqual(self.db.read_document_context(self.source.id), original)
            self.assertEqual(self.db.search_documents("450m"), hits)
            self.assertEqual(self.db.search_documents("Rejected Failed"), ())
            with RecordStore(self.path) as reader:
                self.assertEqual(reader.search_documents("450m"), hits)

    def test_search_returns_the_context_that_matched_despite_concurrent_edit(self):
        self.db.chunk_source(self.source.id)
        original = self.context(title="Oldissuer")
        read_text = self.db.read_source_text
        with RecordStore(self.path) as writer:
            def edit_then_read(*args, **kwargs):
                writer.set_document_context(self.source.id, title="Newissuer", summary="Newsummary",
                                            author_id="bob")
                return read_text(*args, **kwargs)

            with patch.object(self.db, "read_source_text", side_effect=edit_then_read):
                hit, = self.db.search_documents("Oldissuer")
        self.assertEqual((hit.document_context, hit.text), (original, self.text))
        self.assertEqual(self.db.search_documents("Oldissuer"), ())
        self.assertEqual(self.db.search_documents("Newissuer")[0].document_context.title, "Newissuer")

    def test_invalid_writes_preserve_context_and_missing_sources_are_errors(self):
        self.db.chunk_source(self.source.id)
        original = self.context()
        for options in ({"title": 42}, {"summary": None}, {"provenance": []},
                        {"provenance": {"score": float("nan")}}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.context(**options)
        for options in ({"title": " "}, {"author_id": ""}):
            with self.subTest(options=options), self.assertRaises(sqlite3.IntegrityError):
                self.context(**options)
        self.assertEqual(self.db.read_document_context(self.source.id), original)
        self.assertEqual(self.db.search_documents("Acme")[0].document_context, original)
        finding = self.db.create("Finding", text="Finding", author_id="alice")
        source = self.db.create("Source", text="No text", author_id="alice")
        for source_id in ("missing", finding.id, source.id):
            with self.subTest(source_id=source_id):
                with self.assertRaises(KeyError):
                    self.context(source_id)
                with self.assertRaises(KeyError):
                    self.db.read_document_context(source_id)
