from http.client import HTTPConnection
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import threading
import unittest

from stated import LayerWriter, RecordStore
from stated.ui.server import make_server, prepare_workspace


class ServerTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        db_path = self.db_path = Path(directory.name) / "research.sqlite"
        with RecordStore(db_path) as db:
            finding = db.create("Finding", text="Two customers were 22% and 14% of revenue.", author_id="a").id
            self.finding = finding
            self.entity = db.create("Entity", text="PowerFrame Inc. (PowerFrame)", author_id="a").id
            db.create_link(finding, self.entity, "about", author_id="a")
            source = db.ingest_source(description="PowerFrame 10-K", document_text="Sales to one customer were 22%.",
                                      author_id="a").id
            db.patch(finding, author_id="a", citations=[{"source_id": source, "quote": "one customer were 22%"}])
        self.server = make_server(db_path)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def call(self, method, path, body=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, None if body is None else json.dumps(body), {"Content-Type": "application/json"})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, (json.loads(data) if response.getheader("Content-Type") == "application/json" else data)

    def test_the_page_is_served_and_only_reads(self):
        status, page = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"/static/app.js", page)
        self.assertEqual(self.call("GET", "/static/app.js")[0], 200)
        self.assertEqual(self.call("GET", "/static/../server.py")[0], 404)
        self.assertEqual(self.call("GET", "/api/threads")[0], 404)
        self.assertEqual(self.call("POST", "/api/threads", {"question": "Why?"})[0], 501)

    def test_a_record_is_served_with_its_citations_and_connections(self):
        status, record = self.call("GET", f"/api/records/{self.finding[:8]}")
        self.assertEqual((status, record["id"], record["kind"], record["layer"]), (200, self.finding, "Finding", "workspace"))
        # The source has no publication date, so no cutoff sees it; the Finding is seen from its stamp.
        self.assertEqual(record["citations"], [{"quote": "one customer were 22%", "source": record["citations"][0]["source"],
                                                "title": "PowerFrame 10-K", "seen_from": None}])
        self.assertEqual(record["seen_from"], record["as_of"])
        self.assertEqual([(c["relation"], c["direction"], c["record"]["kind"]) for c in record["connections"]],
                         [("about", "out", "Entity"), ("supported_by", "out", "Source")])
        self.assertEqual(record["connections"][0]["record"], {"id": self.entity, "kind": "Entity",
                                                              "text": "PowerFrame Inc. (PowerFrame)", "seen_from": ""})
        self.assertIsNone(record["connections"][1]["record"]["seen_from"])
        self.assertNotIn("citations", record["attributes"])
        status, entity = self.call("GET", f"/api/records/{self.entity}")
        self.assertEqual((entity["connections"][0]["direction"], entity["connections"][0]["record"]["id"]), ("in", self.finding))
        self.assertEqual(self.call("GET", "/api/records/missing")[0], 404)

    def test_features_are_served_with_their_subjects_and_series(self):
        with RecordStore(self.db_path) as db:
            share = db.define_feature(author_id="r", key="top_customer", type="number", instructions="Top customer share.")
        with RecordStore(self.db_path, as_of="2024-02-01") as db:
            value = db.contribute(author_id="agent", records=[{"ref": "v", "kind": "Variable", "text": "22% of sales.",
                "attributes": {"key": "top_customer", "feature": share.id, "value": 0.22}}],
                links=[{"from_id": "v", "to_id": self.entity, "relation": "about"}]).records["v"]
        status, features = self.call("GET", "/api/features")
        self.assertEqual((status, [(f["definition"]["key"], f["subjects"], f["latest"]) for f in features]),
                         (200, [("top_customer", 1, "2024-02-01")]))
        status, feature = self.call("GET", "/api/features/top_customer")
        self.assertEqual((status, feature["definition"]["id"], feature["due"], feature["in_sample"]), (200, share.id, None, 1))
        self.assertEqual(feature["subjects"], [{"subject_id": self.entity, "subject": "PowerFrame Inc. (PowerFrame)",
            "value_id": value.id, "value": 0.22, "as_of": "2024-02-01", "checked": "2024-02-01", "values": 1}])
        status, series = self.call("GET", f"/api/features/{share.id[:8]}/subjects/{self.entity[:8]}")
        self.assertEqual((status, series["checks"], series["points"]), (200, [], [{"id": value.id, "as_of": "2024-02-01",
            "value": 0.22, "author_id": "agent", "in_sample": True}]))
        self.assertEqual(self.call("GET", "/api/features/missing")[0], 404)
        self.assertEqual(self.call("GET", f"/api/features/top_customer/subjects/{self.finding}")[0], 400)
        self.assertEqual(self.call("GET", "/api/features/top_customer/values")[0], 404)

    def test_status_describes_the_workspace(self):
        status, result = self.call("GET", "/api/status")
        self.assertEqual((status, sorted(result)), (200, ["database", "layers", "workspace"]))
        self.assertEqual([(layer["name"], layer["writable"]) for layer in result["layers"]], [("workspace", True)])
        self.assertTrue(result["database"].endswith("research.sqlite"))

    def test_first_run_creates_the_database_and_attaches_a_base(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            with RecordStore(folder / "publisher.sqlite", layer="base") as db:
                db.create("Entity", text="PowerFrame", author_id="a")
                db.export_layer(folder / "base.sqlite", version="2026.10.01")
            db_path, notes = prepare_workspace(folder / "fund" / "research.sqlite")
            self.assertTrue(db_path.is_file())
            self.assertEqual(len(notes), 2)
            self.assertIn("Created your research folder", notes[0])
            self.assertIn("No base layer yet", notes[1])
            db_path, notes = prepare_workspace(db_path, base=folder / "base.sqlite")
            self.assertIn("Attached the base layer, version 2026.10.01", notes[0])
            with RecordStore(db_path) as db:
                self.assertEqual([(l.name, l.version, l.writable) for l in db.layers()],
                                 [("fund", "", True), ("base", "2026.10.01", False)])
            # A base.sqlite beside a new database is attached without being named.
            (folder / "other").mkdir()
            shutil.copyfile(folder / "base.sqlite", folder / "other" / "base.sqlite")
            db_path, notes = prepare_workspace(folder / "other" / "research.sqlite")
            self.assertIn("Attached the base layer", notes[1])
            self.assertEqual(prepare_workspace(db_path)[1], [])  # Nothing to do the second time.

    def test_a_base_made_to_be_the_database_becomes_it(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)

            def base(name):
                writer = LayerWriter(folder / name, name="base", version="2026.11.24", author_id="builder", keyword_index=False)
                writer.entity("11111111-1111-5111-8111-111111111111", "PowerFrame", {"cik": "0000000001"})
                writer.seal()
                return folder / name

            db_path, notes = prepare_workspace(folder / "fund" / "research.sqlite", base=base("download.sqlite"))
            self.assertFalse((folder / "download.sqlite").exists())  # Moved into place, not copied.
            self.assertEqual(len(notes), 2)
            self.assertIn("download.sqlite is now", notes[0])
            self.assertIn("on the base layer, version 2026.11.24", notes[1])
            with RecordStore(db_path) as db:
                self.assertEqual([(l.name, l.version, l.writable) for l in db.layers()],
                                 [("fund", "", True), ("base", "2026.11.24", False)])
                self.assertEqual(db.create("Entity", text="Mine", author_id="a").layer, "fund")
            self.assertEqual(prepare_workspace(db_path)[1], [])
            # An existing research folder is not replaced, and the file is left where it was.
            with self.assertRaisesRegex(ValueError, "already exists"):
                prepare_workspace(db_path, base=base("again.sqlite"))
            self.assertTrue((folder / "again.sqlite").is_file())
            self.assertEqual(sorted(path.name for path in folder.glob("*.sqlite*")), ["again.sqlite"])  # Nothing left beside it.
            # Put in place by hand, it is given the user's layer; left beside a database, it is not attached.
            (folder / "byhand").mkdir()
            shutil.move(base("third.sqlite"), folder / "byhand" / "research.sqlite")
            self.assertIn("on the base layer", prepare_workspace(folder / "byhand" / "research.sqlite")[1][0])
            (folder / "beside").mkdir()
            shutil.move(base("fourth.sqlite"), folder / "beside" / "base.sqlite")
            self.assertIn("No base layer yet", prepare_workspace(folder / "beside" / "research.sqlite")[1][1])



if __name__ == "__main__":
    unittest.main()
