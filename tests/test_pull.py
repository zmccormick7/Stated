from contextlib import redirect_stderr, redirect_stdout
from functools import partial
import gzip
from hashlib import sha256
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from stated import LayerWriter, RecordStore
from stated.cli import main
from stated.embedding import DIMENSIONS

ACME, REPORT, LETTER, NOTICE = (str(uuid5(NAMESPACE_URL, name)) for name in ("acme", "report", "letter", "notice"))


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *arguments):
        pass


class PullTests(unittest.TestCase):
    """A publisher's address lists a layer's versions; a workspace follows the change files from the one it holds."""

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.site = self.directory / "site"
        (self.site / "changes").mkdir(parents=True)
        self.path = self.directory / "research.sqlite"
        shutil.copy(self.write("2026.11.24", REPORT, "Acme Corp 10-K", "2024-02-01"), self.path)
        with RecordStore(self.path) as db:
            db.start_own_layer("fund")
        self.listing = {"layer": "base", "versions": [{"version": "2026.11.24", "file": "base-2026.11.24.sqlite.zst"}]}
        self.publish("2026.11.25", "2026.11.24", LETTER, "Acme Corp 8-K", "2026-11-25")
        self.publish("2026.11.26", "2026.11.25", NOTICE, "Acme Corp DEF 14A", "2026-11-26")
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(self.site)))
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.address = f"http://127.0.0.1:{server.server_port}/"
        self.enterContext(patch.dict(os.environ, {"STATED_FETCH": ""}))

    def write(self, version, record_id, description, published):
        writer = LayerWriter(self.directory / f"{version}.sqlite", name="base", version=version, author_id="builder",
                             written="2026-11-27T00:00:00+00:00", keyword_index=False)
        writer.entity(ACME, "Acme Corp", {"cik": "0000000001"})
        writer.source(record_id, description, f"{description} text.", about=[ACME], attributes={"published": published},
                      vectors=[bytes([5]) * DIMENSIONS], model="test-model")
        return writer.seal().path

    def publish(self, version, follows, *document):
        """Put a day's change file at the address, gzipped, and list it."""
        packed = gzip.compress(Path(self.write(version, *document)).read_bytes())
        file = f"changes/base-{version}.sqlite.gz"
        (self.site / file).write_bytes(packed)
        self.listing["versions"].append({"version": version, "follows": follows, "file": file, "sha256": sha256(packed).hexdigest()})
        self.list()

    def list(self):
        (self.site / "versions.json").write_text(json.dumps(self.listing))

    def pull(self, *arguments):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--db", str(self.path), "pull", "--from", self.address, *arguments])
        return code, json.loads(out.getvalue() or err.getvalue().splitlines()[-1])  # An error is the last line said.

    def held(self):
        with RecordStore(self.path) as db:
            return db.layers()[1].version, db.source_ids_about(ACME)

    def test_each_change_file_since_the_version_held_is_downloaded_and_applied_in_turn(self):
        code, result = self.pull()
        self.assertEqual((code, result["layer"], result["was"], result["version"]), (0, "base", "2026.11.24", "2026.11.26"))
        self.assertEqual([(step["version"], step["records"]["added"]) for step in result["applied"]], [("2026.11.25", 1), ("2026.11.26", 1)])
        self.assertEqual(self.held(), ("2026.11.26", (REPORT, LETTER, NOTICE)))
        with RecordStore(self.path) as db:
            self.assertEqual(db.read_source_text(NOTICE), "Acme Corp DEF 14A text.")
            self.assertEqual(db.count_passages([LETTER, NOTICE]), (2, 2))
        self.assertEqual(self.pull(), (0, {"layer": "base", "was": "2026.11.26", "version": "2026.11.26", "applied": []}))  # Nothing newer.

    def test_a_file_that_is_not_the_one_listed_is_not_applied_and_what_came_before_it_stays(self):
        self.listing["versions"][2]["sha256"] = "0" * 64
        self.list()
        code, result = self.pull()
        self.assertEqual(code, 1)
        self.assertIn("base-2026.11.26.sqlite.gz does not match the hash its list gives", result["error"])
        self.assertEqual(self.held(), ("2026.11.25", (REPORT, LETTER)))

    def test_an_address_that_cannot_bring_this_workspace_forward_says_why(self):
        for change, message in (({"layer": "other"}, "lists versions of the layer 'other', which this workspace does not hold"),
                                ({"versions": self.listing["versions"][2:]}, "does not list version 2026.11.24 of base"),
                                ({"versions": [self.listing["versions"][0], self.listing["versions"][1] | {"file": "http://example.invalid/base.gz"}]},
                                 "names a file outside its own address"),
                                ({"versions": "none"}, "is not a list of a layer's versions")):
            with self.subTest(message):
                (self.site / "versions.json").write_text(json.dumps(self.listing | change))
                code, result = self.pull()
                self.assertEqual(code, 1)
                self.assertIn(message, result["error"])
        (self.site / "versions.json").unlink()
        self.assertIn("could not fetch", self.pull()[1]["error"])
        self.list()
        with patch.dict(os.environ, {"STATED_FETCH": "0"}):  # A feature session reads only what the graph holds.
            self.assertIn("fetching from the web is off", self.pull()[1]["error"])
        self.assertEqual(self.held(), ("2026.11.24", (REPORT,)))


if __name__ == "__main__":
    unittest.main()
