import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest import mock

from stated import RecordStore
from stated.fetch import contact, fetch_page, html_text, pdf_text


def make_pdf(*pages):
    """A small PDF with one line of text on each page (an empty string makes a page with no text)."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>",
               f"<< /Type /Pages /Kids [{' '.join(f'{4 + 2 * i} 0 R' for i in range(len(pages)))}] /Count {len(pages)} >>",
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i, line in enumerate(pages):
        stream = f"BT /F1 12 Tf 72 720 Td ({line}) Tj ET" if line else ""
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {5 + 2 * i} 0 R "
                       "/Resources << /Font << /F1 3 0 R >> >> >>")
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    return out + f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


PDF = make_pdf("Sales to one customer were 22% of revenue.", "Grid approval is pending.")

HTML = """<!DOCTYPE html><html><head><title>PowerFrame 10-Q</title><style>p{color:red}</style>
<script>var x = "<p>not text</p>";</script></head><body><ix:header><p>hidden facts</p></ix:header>
<h1>Customers</h1><p>Sales to one customer were&nbsp;22%   of
revenue.</p><table><tr><th>Customer</th><th>Share</th></tr><tr><td>A</td><td>22%</td></tr></table>
<div>Caf&eacute; &amp; grid</div></body></html>"""
TEXT = "PowerFrame 10-Q\nCustomers\nSales to one customer were 22% of revenue.\nCustomer | Share\nA | 22%\nCafé & grid\n"
PAGES = {"/filing.htm": ("text/html; charset=utf-8", HTML.encode()),
         "/latin.htm": ("text/html; charset=iso-8859-1", "<p>Café</p>".encode("latin-1")),
         "/zipped.htm": ("text/html", gzip.compress(HTML.encode())),
         "/note.txt": ("text/plain", "A€Z\r\nGrid approval is pending.\n".encode()),
         "/report.pdf": ("application/pdf", PDF), "/download": ("application/octet-stream", PDF),
         "/scan.pdf": ("application/pdf", make_pdf("")), "/broken.pdf": ("application/pdf", b"%PDF-1.7 not really"),
         "/sheet.xlsx": ("application/vnd.ms-excel", b"PK"), "/empty.htm": ("text/html", b"<p> </p>")}


class Handler(BaseHTTPRequestHandler):
    agents = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.agents.append(self.headers.get("User-Agent"))
        if self.path == "/moved":
            self.send_response(302)
            self.send_header("Location", "/filing.htm")
            return self.end_headers()
        if self.path not in PAGES:
            return self.send_error(404)
        kind, body = PAGES[self.path]
        self.send_response(200)
        self.send_header("Content-Type", kind)
        if self.path == "/zipped.htm":
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(body)


class FetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    def test_a_page_becomes_the_text_a_reader_sees(self):
        self.assertEqual(html_text(HTML), TEXT)
        page = fetch_page(self.base + "/moved", contact="Ada ada@example.com")
        self.assertEqual((page.url, page.text), (self.base + "/filing.htm", TEXT))
        self.assertRegex(page.retrieved_at, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")
        self.assertNotIn("ada@example.com", " ".join(Handler.agents))  # A contact goes to SEC.gov only.
        self.assertEqual(fetch_page(self.base + "/zipped.htm").text, TEXT)
        self.assertEqual(fetch_page(self.base + "/latin.htm").text, "Café\n")
        self.assertEqual(fetch_page(self.base + "/note.txt").text, "A€Z\r\nGrid approval is pending.\n")  # As served.

    def test_a_pdf_becomes_its_text_page_by_page(self):
        text = "Sales to one customer were 22% of revenue.\n\fGrid approval is pending.\n"
        self.assertEqual(pdf_text(PDF), text)
        self.assertEqual(fetch_page(self.base + "/report.pdf").text, text)
        self.assertEqual(fetch_page(self.base + "/download").text, text)  # Known by its first bytes, not its label.

    def test_what_cannot_be_fetched_says_why(self):
        for url, message in ((self.base + "/sheet.xlsx", "only HTML, plain-text and PDF documents can be fetched; .* is application/vnd.ms-excel"),
                             (self.base + "/scan.pdf", "scan.pdf: the PDF has no text; a scanned document needs OCR"),
                             (self.base + "/broken.pdf", "broken.pdf: the PDF could not be read"),
                             (self.base + "/absent", "could not fetch .*404"), (self.base + "/empty.htm", "has no text"),
                             ("file:///etc/hosts", "must be an http or https address"), (None, "must be an http"),
                             ("https://www.sec.gov/Archives/x.htm", "SEC.gov requires a contact name and email")):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, message):
                fetch_page(url)
        with mock.patch.dict(os.environ, {"STATED_FETCH": "0"}), self.assertRaisesRegex(ValueError, "fetching from the web is off"):
            fetch_page(self.base + "/filing.htm")

    def test_sec_gets_the_contact_as_its_user_agent(self):
        seen = []

        def open_url(request, timeout):
            seen.append(request.get_header("User-agent"))
            raise TimeoutError("stop here")

        with self.assertRaisesRegex(ValueError, "could not fetch"):
            fetch_page("https://www.sec.gov/Archives/x.htm", contact="Ada ada@example.com", open_url=open_url)
        self.assertEqual(seen, ["Ada ada@example.com"])

    def test_ingest_url_stores_the_page_and_where_it_came_from(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "research.sqlite"
            with RecordStore(path):
                pass
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]), "OPENROUTER_API_KEY": "",
                   "STATED_CONTACT": ""}

            def call(*arguments, attributes=None):
                payload = {"description": "PowerFrame 10-Q", "author_id": "a"} | ({"attributes": attributes} if attributes else {})
                result = subprocess.run([sys.executable, "-m", "stated", "--db", str(path), "ingest", *arguments, "--input", "-"],
                                        input=json.dumps(payload), text=True, capture_output=True, env=env)
                return json.loads(result.stdout or result.stderr)

            source = call("--url", self.base + "/moved", attributes={"published": "2026-08-01"})
            self.assertEqual(source["attributes"] | {"retrieved_at": ""},
                             {"url": self.base + "/filing.htm", "retrieved_at": "", "published": "2026-08-01"})
            with RecordStore(path) as db:
                self.assertEqual(db.read_source_text(source["id"]), TEXT)
            self.assertIn("identical text is already ingested", call("--url", self.base + "/filing.htm")["error"])
            self.assertEqual(call()["error"], "give ingest a text file or --url, not both")
            self.assertEqual(call("x.txt", "--url", self.base + "/note.txt")["error"], "give ingest a text file or --url, not both")
            self.assertIn("SEC.gov requires a contact", call("--url", "https://www.sec.gov/x.htm")["error"])
            (Path(directory) / "report.pdf").write_bytes(PDF)  # A PDF on disk is read for its text too.
            filed = call(str(Path(directory) / "report.pdf"))
            with RecordStore(path) as db:
                self.assertTrue(db.read_source_text(filed["id"]).startswith("Sales to one customer were 22% of revenue.\n\f"))
            self.assertIsNone(contact(path))
            (Path(directory) / "contact.txt").write_text("Ada ada@example.com\n")
            self.assertEqual(contact(path), "Ada ada@example.com")
            with mock.patch.dict(os.environ, {"STATED_CONTACT": "Bo bo@example.com"}):
                self.assertEqual(contact(path), "Bo bo@example.com")


if __name__ == "__main__":
    unittest.main()
