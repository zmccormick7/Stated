import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class CLITests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "research.sqlite"
        self.env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        self.call("init")

    def run_cli(self, *arguments, stdin=None):
        return subprocess.run([sys.executable, "-m", "premisedb", "--db", str(self.path), *arguments],
                              input=stdin, text=True, capture_output=True,
                              cwd=self.directory, env=self.env)

    def call(self, *arguments, payload=None, status=0):
        result = self.run_cli(*arguments, stdin=None if payload is None else json.dumps(payload))
        self.assertEqual(result.returncode, status, result.stderr)
        self.assertEqual(result.stderr if status == 0 else result.stdout, "")
        return json.loads(result.stdout if status == 0 else result.stderr)

    def create(self, kind, text):
        return self.call("create", kind, "--input", "-", payload={"text": text, "author_id": "agent-a"})

    def test_recall_and_revision_across_processes(self):
        text = "PowerFrame's backlog may slip.\n€5 and $HOME stay literal."
        data = {"text": text, "attributes": {"basis": "inference"}, "author_id": "agent-a"}
        payload_file = self.directory / "finding.json"
        payload_file.write_text(json.dumps(data), encoding="utf-8")
        finding = self.call("create", "Finding", "--input", str(payload_file))
        question = self.create("Question", "Could grid delays cause deferrals?")
        link = self.call("link", finding["id"], question["id"], "raises", "--input", "-",
                         payload={"author_id": "agent-a", "explanation": "Investigate customer timing."})
        self.assertEqual(self.call("search", "PowerFrame backlog", "--kind", "Finding"), [finding])
        self.assertEqual(self.call("get", finding["id"]), finding)
        self.assertEqual(self.call("get", finding["id"][:8]), finding)
        self.assertEqual(self.call("connections", question["id"][:10])["links"], [link])
        self.assertEqual(self.call("get", "00000000", status=1), {"error": "Not found: 00000000"})
        self.assertEqual(self.call("get-link", link["id"]), link)
        self.assertEqual(self.call("connections", question["id"]),
                         {"links": [link], "has_more": False, "records": {finding["id"]: finding}})
        self.assertEqual(self.call("connections", question["id"], "--direction", "out")["links"], [])
        self.assertEqual(self.call("list", "--kind", "Question", "--author", "agent-a"),
                         {"records": [question], "has_more": False})
        self.assertEqual(self.call("list", "--after", finding["id"], "--limit", "1"),
                         {"records": [question], "has_more": False})
        self.assertEqual(self.call("list", "--where", "basis=inference")["records"], [finding])
        self.assertEqual(self.call("list", "--where", "basis=\"inference\"", "--where", "status=open")["records"], [])
        self.assertEqual(self.run_cli("list", "--where", "novalue").returncode, 2)
        walked = self.call("expand", question["id"], "--relation", "raises")
        self.assertEqual(walked["reached"][1], {
            "id": finding["id"], "kind": "Finding", "text": finding["text"], "depth": 1,
            "path": [{"id": link["id"], "from_id": finding["id"], "to_id": question["id"], "relation": "raises"}]})
        self.assertEqual((walked["reached"][0]["depth"], walked["capped"], walked["has_more"]), (0, [], False))
        full = self.call("expand", question["id"], "--relation", "raises", "--full")
        self.assertEqual((full["reached"][1]["record"], full["reached"][1]["path"]), (finding, [link]))
        self.assertEqual(self.run_cli("expand", question["id"], "--depth", "3").returncode, 2)
        self.assertEqual(self.call("relations"),
                         [{"relation": "raises", "from_kind": "Finding", "to_kind": "Question", "links": 1}])
        relabelled = self.call("patch-link", link["id"], "--input", "-", payload={"author_id": "agent-b", "relation": "qualifies"})
        self.assertEqual((relabelled["relation"], relabelled["explanation"]), ("qualifies", link["explanation"]))
        changed = self.call("update", finding["id"], "--input", "-",
                            payload={"text": "PowerFrame ramp delayed.", "author_id": "agent-b"})
        self.assertEqual((changed["id"], changed["attributes"], changed["author_id"]),
                         (finding["id"], {}, "agent-b"))
        self.assertEqual(self.call("search", "backlog"), [])
        patched = self.call("patch", finding["id"], "--input", "-", payload={
            "author_id": "agent-c", "attributes": {"basis": "inference", "status": "draft"}, "remove": ["absent"]})
        self.assertEqual((patched["text"], patched["attributes"], patched["author_id"]),
                         ("PowerFrame ramp delayed.", {"basis": "inference", "status": "draft"}, "agent-c"))
        changed = self.call("patch", finding["id"], "--input", "-", payload={"author_id": "agent-b", "remove": ["status"]})
        self.assertEqual(changed["attributes"], {"basis": "inference"})
        self.call("patch", finding["id"], "--input", "-", payload={"author_id": "agent-b"}, status=1)
        self.call("init")  # Opening an existing workspace must retain its records.
        self.assertEqual(self.call("get", finding["id"]), changed)

    def test_contribute_writes_a_batch_by_ref(self):
        entity = self.create("Entity", "PowerFrame (fictional)")
        result = self.call("contribute", "--input", "-", payload={
            "author_id": "agent-a",
            "records": [{"ref": "f", "kind": "Finding", "text": "Backlog may slip."},
                        {"ref": "q", "kind": "Question", "text": "Can customers defer?"}],
            "links": [{"from_id": "f", "to_id": entity["id"], "relation": "about"},
                      {"from_id": "f", "to_id": "q", "relation": "raises"}]})
        self.assertEqual(sorted(result["records"]), ["f", "q"])
        self.assertEqual([link["to_id"] for link in result["links"]], [entity["id"], result["records"]["q"]["id"]])
        self.assertEqual(self.call("get", result["records"]["f"]["id"]), result["records"]["f"])
        self.call("contribute", "--input", "-", status=1, payload={
            "author_id": "agent-a", "records": [{"ref": "g", "kind": "Finding", "text": "Orphan"}],
            "links": [{"from_id": "g", "to_id": "missing", "relation": "about"}]})
        self.assertEqual(self.call("search", "Orphan"), [])

    def test_connection_limits_and_link_updates(self):
        finding = self.create("Finding", "Capacity depends on power.")
        question = self.create("Question", "Is power available?")
        links = [self.call("link", finding["id"], question["id"], relation, "--input", "-",
                           payload={"author_id": "agent-a"}) for relation in ("raises", "depends_on")]
        self.assertEqual(self.call("connections", finding["id"], "--limit", "1"),
                         {"links": links[:1], "has_more": True, "records": {question["id"]: question}})
        self.assertEqual(self.call("connections", finding["id"], "--relation", "depends_on")["links"], links[1:])
        fields = {"from_id": finding["id"], "to_id": question["id"], "relation": "concerns", "author_id": "agent-b"}
        changed = self.call("update-link", links[0]["id"], "--input", "-", payload=fields)
        self.call("update-link", changed["id"], "--input", "-", payload={**fields, "to_id": "missing"}, status=1)
        self.assertEqual(self.call("get-link", changed["id"]), changed)

    def test_exact_source_ingestion_and_context(self):
        text = "A€🙂Z\r\nGrid approval is pending.\n"
        source_file = self.directory / "filing.txt"
        source_file.write_bytes(text.encode("utf-8"))
        source = self.call("ingest", str(source_file), "--input", "-",
                           payload={"description": "Fictional filing", "author_id": "agent-a"})
        sid = source["id"]
        self.assertEqual(self.call("source-text", sid), text)
        self.assertEqual(self.call("source-text", sid, "--start-byte", "1", "--end-byte", "8"), "€🙂")
        self.call("source-text", sid, "--start-byte", "2", status=1)
        self.assertIsNone(self.call("get-context", sid))
        self.assertGreater(self.call("chunk", sid, "--max-chars", "12"), 1)
        context = self.call("set-context", sid, "--input", "-", payload={
            "title": "PowerFrame Q2", "summary": "Fictional filing about utility approval.",
            "author_id": "agent-a", "provenance": {"method": "agent-authored"}})
        self.assertEqual(self.call("get-context", sid), context)
        self.assertEqual(self.call("source-text", sid), text)
        match = {"source_id": sid, "start_byte": 16, "end_byte": 30, "text": "approval is pe",
                 "document_context": None}
        self.assertEqual(self.call("locate", sid, "approval is pe"), [match])
        self.assertEqual(self.call("locate", sid, "approval  is"), [])
        stdin = self.run_cli("locate", sid, "-", stdin="Z\r\nGrid approval\n")
        self.assertEqual([(m["start_byte"], m["end_byte"]) for m in json.loads(stdin.stdout)], [(8, 24)])
        self.call("locate", sid, " ", status=1)
        finding = self.call("create", "Finding", "--input", "-", payload={
            "text": "Approval is pending.", "author_id": "agent-a",
            "citations": [{"source_id": sid, "quote": "Grid approval is pending."}]})
        self.assertEqual(finding["attributes"]["citations"],
                         [{"source_id": sid, "start_byte": 11, "end_byte": 36, "quote": "Grid approval is pending."}])
        self.assertEqual([link["to_id"] for link in self.call("connections", finding["id"])["links"]], [sid])
        cited = self.call("cited-by", sid, "--start-byte", "20", "--end-byte", "24")
        self.assertEqual(cited, {"citations": [{"record": finding, "source_id": sid, "start_byte": 11, "end_byte": 36}],
                                 "has_more": False})
        self.assertEqual(self.call("cited-by", sid, "--end-byte", "11")["citations"], [])

    def test_bad_inputs_fail_without_writes_or_tracebacks(self):
        for payload in ([], {"text": "Missing author"}, {"text": "Claim", "author_id": "a", "kind": "Entity"},
                        {"text": "Claim", "author_id": "a", "attributes": []}):
            with self.subTest(payload=payload):
                error = self.call("create", "Finding", "--input", "-", payload=payload, status=1)
                self.assertTrue(error["error"])
        invalid = self.run_cli("create", "Finding", "--input", "-", stdin="{invalid")
        self.assertEqual(invalid.returncode, 1)
        self.assertEqual(invalid.stdout, "")
        self.assertTrue(json.loads(invalid.stderr)["error"])
        self.assertEqual(self.call("search", "Claim Missing"), [])
        self.call("create", "Finding", "--input", "absent.json", status=1)
        self.call("get", "missing", status=1)
        usage = self.run_cli("search", "claim", "--limit", "invalid")
        self.assertEqual((usage.returncode, usage.stdout), (2, ""))

    def test_only_init_creates_a_database(self):
        self.path = self.directory / "typo.sqlite"
        self.assertIn("init", self.call("search", "anything", status=1)["error"])
        self.assertFalse(self.path.exists())
        help_result = self.run_cli("--help")
        self.assertEqual(help_result.returncode, 0)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.call("init"), {"database": str(self.path.resolve())})
        self.path = Path(":memory:")
        self.assertIn("persistent", self.call("init", status=1)["error"])

    def test_environment_selects_the_default_database(self):
        shared = self.directory / "shared.sqlite"
        run = lambda *arguments, path=str(shared): subprocess.run(
            [sys.executable, "-m", "premisedb", *arguments], text=True, capture_output=True,
            cwd=self.directory, env={**self.env, "PREMISEDB_DB": path})
        self.assertEqual(json.loads(run("init").stdout), {"database": str(shared.resolve())})
        self.assertEqual(json.loads(run("search", "anything").stdout), [])
        self.assertIn("does not exist", run("--db", "other.sqlite", "search", "x").stderr)
        self.assertEqual(json.loads(run("init", path="").stdout),
                         {"database": str((self.directory / "research.sqlite").resolve())})
