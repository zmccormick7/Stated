from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stated import RecordStore


TABLE = "﻿ticker,period,revenue\r\nACME,2024Q1,193479000\r\nBETA,2024Q1,\"1,204\"\r\n"


class TableSourceTests(unittest.TestCase):
    """A CSV table is a Source: one dated release of structured data, cited row by row."""

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.db = RecordStore(self.path)
        self.addCleanup(self.db.close)

    def ingest(self, text=TABLE, **attributes):
        return self.db.ingest_table(description="Quarterly revenue", table_text=text, author_id="loader",
                                    attributes={"published": "2024-05-02", "dataset": "revenue"} | attributes)

    def test_a_table_is_a_dated_source_kept_byte_for_byte(self):
        table = self.ingest()
        self.assertEqual((table.kind, table.text), ("Source", "Quarterly revenue"))
        self.assertEqual(table.attributes, {"published": "2024-05-02", "dataset": "revenue", "format": "csv",
                                            "columns": ["ticker", "period", "revenue"], "rows": 2})
        self.assertEqual(self.db.read_source_text(table.id), TABLE)

    def test_a_variable_cites_the_row_it_came_from(self):
        table = self.ingest()
        value = self.db.create("Variable", text="ACME revenue, 2024Q1", author_id="loader",
                               attributes={"value": 193479000, "units": "USD"},
                               citations=[{"source_id": table.id, "quote": "ACME,2024Q1,193479000"}])
        citation = value.attributes["citations"][0]
        self.assertEqual(self.db.read_source_text(table.id, start_byte=citation["start_byte"],
                                                  end_byte=citation["end_byte"]), "ACME,2024Q1,193479000")
        self.assertEqual([link.to_id for link in self.db.connections(value.id, relation="supported_by").links],
                         [table.id])

    def test_a_table_is_seen_from_its_release_date(self):
        table = self.ingest()
        with RecordStore(self.path, as_of="2024-05-01") as before, self.assertRaisesRegex(KeyError, "not visible"):
            before.read_source_text(table.id)
        with RecordStore(self.path, as_of="2024-05-02") as on:
            self.assertEqual(on.read_source_text(table.id), TABLE)
        with RecordStore(self.path, as_of="2024-05-01") as before, self.assertRaisesRegex(ValueError, "on or before"):
            before.ingest_table(description="Later release", table_text="a,b\n1,2\n", author_id="loader",
                                attributes={"published": "2024-06-01"})

    def test_a_table_without_a_date_or_a_citable_shape_is_refused(self):
        cases = {
            "needs attributes.published": ("a,b\n1,2\n", {"published": None}),
            "needs attributes.published, the date": ("a,b\n1,2\n", {"published": "soon"}),
            "header row": ("\n1,2\n", {}),
            "nonblank and distinct": ("a,a\n1,2\n", {}),
            "nonblank and distinct: \\['a', ' '\\]": ("a, \n1,2\n", {}),
            "row 3 has 1 fields where the header names 2": ("a,b\n1,2\n3\n", {}),
            "remove blank lines": ("a,b\n1,2\n\n3,4\n", {}),
            "at least one data row": ("a,b\n", {}),
            "not valid CSV": ('a,b\n"1"2,3\n', {}),
            "sets columns itself": ("a,b\n1,2\n", {"columns": ["a", "b"]}),
        }
        for message, (text, attributes) in cases.items():
            with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                self.ingest(text, **attributes)
        self.assertEqual(self.db.list_records(kind="Source").records, ())

    def test_a_tables_shape_is_fixed_and_it_is_not_chunked(self):
        table = self.ingest()
        with self.assertRaisesRegex(ValueError, "read by its rows"):
            self.db.chunk_source(table.id)
        for change in ({"attributes": {"columns": ["ticker"]}}, {"remove": ["format"]}):
            with self.subTest(change), self.assertRaisesRegex(ValueError, "fixed with its text"):
                self.db.patch(table.id, author_id="loader", **change)
        with self.assertRaisesRegex(ValueError, "fixed with its text"):
            self.db.update(table.id, text="Quarterly revenue", author_id="loader",
                           attributes={"published": "2024-05-02"})
        self.assertEqual(self.db.patch(table.id, author_id="loader", attributes={"vendor": "SEC"}).attributes["vendor"],
                         "SEC")
        document = self.db.ingest_source(description="A note", document_text="a,b\n1,2\n", author_id="loader")
        with self.assertRaisesRegex(ValueError, "fixed with its text"):
            self.db.patch(document.id, author_id="loader", attributes={"format": "csv"})


if __name__ == "__main__":
    unittest.main()
