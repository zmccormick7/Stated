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

from premisedb import RecordStore
from premisedb.fetch import contact, fetch_page, html_text

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
         "/report.pdf": ("application/pdf", b"%PDF-1.7"), "/empty.htm": ("text/html", b"<p> </p>")}


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

    def test_what_cannot_be_fetched_says_why(self):
        for url, message in ((self.base + "/report.pdf", "only HTML and plain-text pages can be fetched; .* is application/pdf"),
                             (self.base + "/absent", "could not fetch .*404"), (self.base + "/empty.htm", "has no text"),
                             ("file:///etc/hosts", "must be an http or https address"), (None, "must be an http"),
                             ("https://www.sec.gov/Archives/x.htm", "SEC.gov requires a contact name and email")):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, message):
                fetch_page(url)
        with mock.patch.dict(os.environ, {"PREMISEDB_FETCH": "0"}), self.assertRaisesRegex(ValueError, "fetching from the web is off"):
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
                   "PREMISEDB_CONTACT": ""}

            def call(*arguments, attributes=None):
                payload = {"description": "PowerFrame 10-Q", "author_id": "a"} | ({"attributes": attributes} if attributes else {})
                result = subprocess.run([sys.executable, "-m", "premisedb", "--db", str(path), "ingest", *arguments, "--input", "-"],
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
            self.assertIsNone(contact(path))
            (Path(directory) / "contact.txt").write_text("Ada ada@example.com\n")
            self.assertEqual(contact(path), "Ada ada@example.com")
            with mock.patch.dict(os.environ, {"PREMISEDB_CONTACT": "Bo bo@example.com"}):
                self.assertEqual(contact(path), "Bo bo@example.com")


if __name__ == "__main__":
    unittest.main()
