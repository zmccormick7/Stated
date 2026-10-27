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
        # No OpenRouter key, so ingest makes no model calls; test_context covers document context.
        self.env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]), "OPENROUTER_API_KEY": ""}
        self.call("init")

    def run_cli(self, *arguments, stdin=None):
        return subprocess.run([sys.executable, "-m", "stated", "--db", str(self.path), *arguments],
                              input=stdin, text=True, capture_output=True,
                              cwd=self.directory, env=self.env)

    def call(self, *arguments, payload=None, status=0):
        result = self.run_cli(*arguments, stdin=None if payload is None else json.dumps(payload))
        self.assertEqual(result.returncode, status, result.stderr)
        self.assertEqual(result.stderr if status == 0 else result.stdout, "")
        return json.loads(result.stdout if status == 0 else result.stderr)

    def create(self, kind, text):
        return self.call("create", kind, "--input", "-", payload={"text": text, "author_id": "agent-a"})

    @staticmethod
    def edits(record):
        """The edit details a preview ends with."""
        return {"author_id": record["author_id"], "updated_at": record["updated_at"], "as_of": record["as_of"],
                "layer": record["layer"]}

    def test_recall_and_revision_across_processes(self):
        text = "PowerFrame's backlog may slip.\n€5 and $HOME stay literal."
        data = {"text": text, "attributes": {"basis": "inference"}, "author_id": "agent-a"}
        payload_file = self.directory / "finding.json"
        payload_file.write_text(json.dumps(data), encoding="utf-8")
        finding = self.call("create", "Finding", "--input", str(payload_file))
        question = self.create("Question", "Could grid delays cause deferrals?")
        link = self.call("link", finding["id"], question["id"], "raises", "--input", "-",
                         payload={"author_id": "agent-a", "explanation": "Investigate customer timing."})
        linked = {"id": finding["id"], "kind": "Finding", "text": finding["text"], "basis": "inference"}
        preview = linked | self.edits(finding)
        self.assertEqual(self.call("search", "PowerFrame backlog", "--kind", "Finding"), {"records": [preview]})
        self.assertEqual(self.call("search", "PowerFrame backlog", "--kind", "Finding", "--full"), {"records": [finding]})
        self.assertEqual(self.call("get", finding["id"]), finding)
        self.assertEqual(self.call("get", finding["id"][:8]), finding)
        self.assertEqual(self.call("connections", question["id"][:10], "--full")["links"], [link])
        self.assertEqual(self.call("get", "00000000", status=1), {"error": "Not found: 00000000"})
        self.assertEqual(self.call("get-link", link["id"]), link)
        self.assertEqual(self.call("connections", question["id"]),
                         {"links": [link | {"other": linked}], "has_more": False})
        self.assertEqual(self.call("connections", question["id"], "--full"),
                         {"links": [link], "has_more": False, "records": [finding]})
        self.assertEqual(self.call("connections", question["id"], "--direction", "out")["links"], [])
        self.assertEqual(self.call("list", "--kind", "Question", "--author", "agent-a", "--full"),
                         {"records": [question], "has_more": False})
        self.assertEqual(self.call("list", "--after", finding["id"], "--limit", "1"),
                         {"records": [{"id": question["id"], "kind": "Question", "text": question["text"],
                                       **self.edits(question)}],
                          "has_more": False})
        self.assertEqual(self.call("list", "--where", "basis=inference")["records"], [preview])
        self.assertEqual(self.call("list", "--where", "basis=\"inference\"", "--where", "status=open")["records"], [])
        self.assertEqual(self.run_cli("list", "--where", "novalue").returncode, 2)
        walked = self.call("expand", question["id"], "--relation", "raises")
        self.assertEqual(walked["reached"][1], {
            **preview, "depth": 1,
            "path": [{"id": link["id"], "from_id": finding["id"], "to_id": question["id"], "relation": "raises"}]})
        self.assertEqual((walked["reached"][0]["depth"], walked["capped"], walked["has_more"]), (0, [], False))
        full = self.call("expand", question["id"], "--relation", "raises", "--full")
        self.assertEqual((full["reached"][1]["record"], full["reached"][1]["path"]), (finding, [link]))
        self.assertEqual(self.run_cli("expand", question["id"], "--depth", "3").returncode, 2)
        self.assertEqual(self.call("relations"),
                         {"relations": [{"relation": "raises", "from_kind": "Finding", "to_kind": "Question", "links": 1}]})
        relabelled = self.call("patch-link", link["id"], "--input", "-", payload={"author_id": "agent-b", "relation": "qualifies"})
        self.assertEqual((relabelled["relation"], relabelled["explanation"]), ("qualifies", link["explanation"]))
        changed = self.call("update", finding["id"], "--input", "-",
                            payload={"text": "PowerFrame ramp delayed.", "author_id": "agent-b"})
        self.assertEqual((changed["id"], changed["attributes"], changed["author_id"]),
                         (finding["id"], {}, "agent-b"))
        self.assertEqual(self.call("search", "backlog"), {"records": []})
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
        self.assertEqual(self.call("search", "Orphan"), {"records": []})

    def test_previews_show_telling_attributes_and_truncate_text(self):
        text = "Growth input " + "x" * 300
        variable = self.call("create", "Variable", "--input", "-", payload={"text": text, "author_id": "agent-a",
            "attributes": {"key": "growth", "value": [0.1, 0.2], "basis": "assumption", "model_ref": "m.py"}})
        preview = self.call("list", "--kind", "Variable")["records"][0]
        self.assertEqual(preview, {"id": variable["id"], "kind": "Variable", "text": text[:199] + "…",
                                   "basis": "assumption", "key": "growth", "value": [0.1, 0.2], **self.edits(variable)})
        self.assertEqual(self.call("list", "--kind", "Variable", "--full")["records"], [variable])
        edited = self.call("patch", variable["id"], "--input", "-", payload={"author_id": "agent-b", "attributes": {"basis": "inference"}})
        self.assertEqual(self.call("list", "--kind", "Variable")["records"][0], {
            **preview, "basis": "inference", "author_id": "agent-b", "created_by": "agent-a", "updated_at": edited["updated_at"],
            "layer": "workspace"})

    def test_recall_returns_records_evidence_and_issues(self):
        text = "A€ backlog note. Customers may defer deliveries."
        source_file = self.directory / "note.txt"
        source_file.write_bytes(text.encode("utf-8"))
        source = self.call("ingest", str(source_file), "--input", "-", payload={"description": "Note", "author_id": "agent-a"})
        finding = self.call("create", "Finding", "--input", "-", payload={
            "text": "Backlog may slip.", "author_id": "agent-a", "attributes": {"basis": "disclosed"},
            "citations": [{"source_id": source["id"], "quote": "Customers may defer deliveries."}]})
        question = self.create("Question", "Can customers defer?")
        self.call("link", finding["id"], question["id"], "raises", "--input", "-", payload={"author_id": "agent-a"})
        result = self.call("recall", "backlog", "--max-records", "5")
        entry, walked = result["records"]
        self.assertEqual(entry, {"id": finding["id"], "kind": "Finding", "text": "Backlog may slip.", "basis": "disclosed",
                                 "citations": 1, "entry_channels": ["search"]})
        self.assertEqual(walked, {"id": question["id"], "kind": "Question", "text": "Can customers defer?",
                                  "via": finding["id"][:8], "path": ["raises"]})
        self.assertEqual(result["evidence"], [{"source_id": source["id"], "start_byte": 19, "end_byte": 50,
                                               "quote": "Customers may defer deliveries.", "record_ids": [finding["id"][:8]]}])
        self.assertEqual((result["issues"], result["frontier"]), ({"links": [], "open_questions": []}, []))
        seeded = self.call("recall", "nothing", "--seed", question["id"][:8], "--max-records", "1")
        self.assertEqual(seeded["records"][0]["id"], question["id"])
        self.assertEqual(seeded["frontier"], [{"id": finding["id"], "kind": "Finding", "text": "Backlog may slip."}])
        self.assertTrue(seeded["coverage"]["truncated"])
        self.assertEqual(seeded["issues"]["open_questions"], [])  # The Question is already a record.
        full = self.call("recall", "backlog", "--full")
        self.assertEqual(full["records"][0]["record"], finding)
        self.call("contribute", "--input", "-", payload={"author_id": "agent-a", "records": [
            {"ref": str(n), "kind": "Finding", "text": f"Deferral clause {n}. " * 10} for n in range(12)], "links": [
            {"from_id": finding["id"][:8], "to_id": str(n), "relation": "depends_on"} for n in range(12)] + [
            {"from_id": str(n), "to_id": str((n + 1) % 12), "relation": "qualifies", "explanation": "Terms vary. " * 15}
            for n in range(12)]})
        small = self.call("recall", "backlog", "--max-chars", "2000")
        self.assertLessEqual(len(json.dumps(small)), 2000)
        self.assertGreater(small["coverage"]["dropped_for_size"], 0)
        self.assertTrue(small["coverage"]["truncated"])
        self.assertEqual(small["records"][0]["id"], finding["id"])
        kept = {r["id"] for r in small["records"]}
        self.assertTrue(small["issues"]["links"])
        self.assertTrue(all({l["from_id"], l["to_id"]} & kept for l in small["issues"]["links"]))  # Dropped with records.
        self.assertEqual(self.run_cli("recall", "backlog", "--max-chars", "100").returncode, 1)

    def test_connection_limits_and_link_updates(self):
        finding = self.create("Finding", "Capacity depends on power.")
        question = self.create("Question", "Is power available?")
        links = [self.call("link", finding["id"], question["id"], relation, "--input", "-",
                           payload={"author_id": "agent-a"}) for relation in ("raises", "depends_on")]
        self.assertEqual(self.call("connections", finding["id"], "--limit", "1", "--full"),
                         {"links": links[:1], "has_more": True, "records": [question]})
        self.assertEqual(self.call("connections", question["id"], "--limit", "1")["links"][0]["other"],
                         {"id": finding["id"], "kind": "Finding", "text": finding["text"]})
        self.assertEqual(self.call("connections", finding["id"], "--relation", "depends_on", "--full")["links"], links[1:])
        fields = {"from_id": finding["id"], "to_id": question["id"], "relation": "concerns", "author_id": "agent-b"}
        changed = self.call("update-link", links[0]["id"], "--input", "-", payload=fields)
        self.call("update-link", changed["id"], "--input", "-", payload={**fields, "to_id": "missing"}, status=1)
        self.assertEqual(self.call("get-link", changed["id"]), changed)
        listed = self.call("links", "--since", changed["updated_at"], "--relation", "concerns")
        self.assertEqual(listed, {"links": [changed | {"from": {"id": finding["id"], "kind": "Finding", "text": finding["text"]},
                                                       "to": {"id": question["id"], "kind": "Question", "text": question["text"]}}],
                                  "has_more": False})
        self.assertEqual(self.call("links", "--created-by", "agent-a", "--after", links[0]["id"][:8], "--full"),
                         {"links": links[1:], "has_more": False})
        self.assertEqual(self.call("link", finding["id"], question["id"], "depends_on", "--input", "-",
                                   payload={"author_id": "agent-b"}, status=1),
                         {"error": f"an identical depends_on link already exists: {links[1]['id']}; patch it instead"})

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
        self.assertEqual(self.call("locate", sid, "approval is pe"), {"matches": [match]})
        self.assertEqual(self.call("locate", sid, "approval  is"), {"matches": []})
        stdin = self.run_cli("locate", sid, "-", stdin="Z\r\nGrid approval\n")
        self.assertEqual([(m["start_byte"], m["end_byte"]) for m in json.loads(stdin.stdout)["matches"]], [(8, 24)])
        self.call("locate", sid, " ", status=1)
        finding = self.call("create", "Finding", "--input", "-", payload={
            "text": "Approval is pending.", "author_id": "agent-a",
            "citations": [{"source_id": sid, "quote": "Grid approval is pending."}]})
        self.assertEqual(finding["attributes"]["citations"],
                         [{"source_id": sid, "start_byte": 11, "end_byte": 36, "quote": "Grid approval is pending."}])
        self.assertEqual(self.call("search", "Approval pending", "--kind", "Finding"),
                         {"records": [{"id": finding["id"], "kind": "Finding", "text": "Approval is pending.", "citations": 1,
                                       **self.edits(finding)}]})
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
        self.assertEqual(self.call("search", "Claim Missing"), {"records": []})
        self.call("create", "Finding", "--input", "absent.json", status=1)
        self.call("get", "missing", status=1)
        usage = self.run_cli("search", "claim", "--limit", "invalid")
        self.assertEqual((usage.returncode, usage.stdout), (2, ""))

    def test_init_names_the_layer_that_layers_and_records_report(self):
        self.path = self.directory / "base.sqlite"
        self.call("init", "--layer", "base")
        self.assertEqual(self.call("layers"), {"layers": [{"name": "base", "version": "", "writable": True,
                                                             "content_sha256": None, "exported_at": None}]})
        self.assertEqual(self.create("Entity", "PowerFrame")["layer"], "base")
        self.assertEqual(self.call("init", "--layer", "ignored"), {"database": str(self.path.resolve())})
        self.assertEqual(self.call("layers")["layers"][0]["name"], "base")
        published = self.directory / "base-2026.09.30.sqlite"
        export = self.call("export-layer", str(published), "--version", "2026.09.30")
        self.assertEqual({key: export[key] for key in ("name", "version", "records", "links", "sources", "withheld")},
                         {"name": "base", "version": "2026.09.30", "records": 1, "links": 0, "sources": 0,
                          "withheld": 0})
        self.path = published  # A published file answers every read command.
        self.assertEqual(self.call("layers")["layers"][0]["version"], "2026.09.30")
        self.assertEqual(self.call("search", "PowerFrame")["records"][0]["layer"], "base")
        self.assertIn("layer", self.call("create", "Entity", "--input", "-",
                                         payload={"text": "Nope", "author_id": "a"}, status=1)["error"])
        self.path = self.directory / "fund.sqlite"
        self.call("init", "--layer", "fund")
        attached = self.call("attach-layer", str(published))
        self.assertEqual((attached["name"], attached["version"], attached["records"]["added"], attached["review"]),
                         ("base", "2026.09.30", 1, {}))
        self.assertEqual([layer["name"] for layer in self.call("layers")["layers"]], ["fund", "base"])
        self.assertEqual(self.call("search", "PowerFrame")["records"][0]["layer"], "base")
        self.assertEqual(self.create("Question", "Which plants?")["layer"], "fund")

    def test_check_model_reports_each_output_formula(self):
        model = self.create("Model", "Revenue model")
        payload = {"author_id": "agent-a", "records": [
            {"ref": "gw", "kind": "Variable", "text": "GW", "attributes": {"key": "gw", "role": "input", "value": [20, 24]}},
            {"ref": "price", "kind": "Variable", "text": "Price", "attributes": {"key": "price", "role": "input", "value": 570}},
            {"ref": "rev", "kind": "Variable", "text": "Revenue", "attributes": {
                "key": "revenue", "role": "output", "value": [11400, 13680], "formula": "gw * price"}}],
            "links": [{"from_id": ref, "to_id": model["id"], "relation": "in_model"} for ref in ("gw", "price", "rev")]
            + [{"from_id": "rev", "to_id": ref, "relation": "depends_on"} for ref in ("gw", "price")]}
        self.call("contribute", "--input", "-", payload=payload)
        result = self.call("check-model", model["id"][:8], "--tolerance", "0.001")
        self.assertEqual((result["model_id"], result["inputs"], result["outputs"], result["without_formula"]),
                         (model["id"], 2, 1, []))
        self.assertEqual(result["checked"], [{"key": "revenue", "stored": [11400, 13680], "computed": [11400, 13680],
                                              "matches": True, "exact": True, "problems": []}])
        self.assertIn("not a Model", self.call("check-model", self.create("Finding", "x")["id"], status=1)["error"])
        scenario = self.call("evaluate-model", model["id"][:8], "--set", "price=600", "--set", "gw=[10, 24]")
        self.assertEqual(scenario["overrides"], {"price": 600, "gw": [10, 24]})
        self.assertEqual(scenario["variables"][-1], {"key": "revenue", "role": "output", "stored": [11400, 13680],
                                                     "value": [6000, 14400], "changed": True})
        self.assertFalse(any(v["changed"] for v in self.call("evaluate-model", model["id"])["variables"]))
        self.assertIn("not in the model", self.call("evaluate-model", model["id"], "--set", "tariff=1", status=1)["error"])

    def test_check_answer_reports_problems_without_failing(self):
        finding = self.create("Finding", "Two customers were 22% and 14% of revenue.")
        answer = {"title": "Two customers were 36% of revenue.", "body": "One was 22%. [1]",
                  "sources": [{"record": finding["id"][:8]}],
                  "blocks": [{"type": "chart", "unit": "%", "rows": [{"label": "2026", "parts": [
                      {"label": "Largest", "record": finding["id"], "value": 22, "as_written": "22%"}]}]}]}
        result = self.call("check-answer", "--input", "-", payload=answer)
        self.assertEqual((result["ok"], result["problems"]), (True, []))
        self.assertEqual(result["answer"]["sources"], [{"record": finding["id"], "kind": "Finding",
                                                        "text": "Two customers were 22% and 14% of revenue."}])
        answer["blocks"][0]["rows"][0]["parts"][0] |= {"value": 24, "as_written": "24%"}
        result = self.call("check-answer", "--input", "-", payload=answer)
        self.assertEqual((result["ok"], result["answer"]), (False, None))
        self.assertIn('"24%" does not appear', result["problems"][0])

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

    def test_reads_at_a_cutoff_from_the_option_or_the_environment(self):
        text = self.directory / "release.txt"
        text.write_text("The bank was closed on March 10.", encoding="utf-8")
        source = self.call("ingest", str(text), "--input", "-", payload={
            "description": "Release", "author_id": "agent-a", "attributes": {"published": "2023-03-10"}})
        self.assertEqual(self.call("--as-of", "2023-03-10", "get", source["id"]), source)
        self.assertEqual(self.call("--as-of", "2023-02-24", "get", source["id"], status=1),
                         {"error": f"Not found: {source['id']} is not visible as of 2023-02-24"})
        self.assertEqual(self.call("--as-of", "2023-02-24", "list", "--kind", "Source"), {"records": [], "has_more": False})
        self.assertEqual(self.call("--as-of", "24/02/2023", "list", status=1),
                         {"error": "as_of must be a date, like 2023-02-24, or None"})
        self.env["STATED_AS_OF"] = "2023-02-24"
        finding = self.create("Finding", "Written at the cutoff.")
        self.assertEqual(finding["as_of"], "2023-02-24")
        self.assertEqual(self.call("search", "Release"), {"records": []})
        self.assertEqual(self.call("--as-of", "2023-03-10", "search", "Release", status=1),
                         {"error": "the cutoff is fixed at 2023-02-24 by STATED_AS_OF"})
        self.assertEqual(self.call("ingest", "--url", "https://example.com/page", "--input", "-", status=1,
                                   payload={"description": "Page", "author_id": "agent-a"}),
                         {"error": "ingest --url is off at a cutoff: a fetched page carries today's date; "
                                   "ingest a file with its publication date in attributes.published"})
        self.env["STATED_AS_OF"] = ""  # Empty means no cutoff, as an unset variable does.
        self.assertEqual(self.call("search", "Release", "--full"), {"records": [source]})

    def test_fill_model_and_state_export(self):
        bank = self.create("Entity", "A bank")
        keys = {}
        for key, attributes in (("a", {"value": 2}), ("b", {"value": None, "formula": "a / 3"})):
            definition = self.call("define-feature", "--input", "-", payload={
                "author_id": "r", "key": key, "type": "number", "instructions": f"The {key}.",
                **({"formula": attributes["formula"]} if "formula" in attributes else {})})
            keys[key] = self.call("create", "Variable", "--input", "-", payload={
                "text": key, "author_id": "agent-a", "attributes": {"key": key, "feature": definition["id"], **attributes}})
            self.call("link", keys[key]["id"], bank["id"], "about", "--input", "-", payload={"author_id": "agent-a"})
        self.call("link", keys["b"]["id"], keys["a"]["id"], "depends_on", "--input", "-", payload={"author_id": "agent-a"})
        filled = self.call("fill-model", keys["b"]["id"][:8], "--input", "-", payload={"author_id": "agent-a"})
        self.assertEqual(filled, {"model_id": keys["b"]["id"],
                                  "written": [{"key": "b", "stored": None, "value": 2 / 3, "id": keys["b"]["id"]}]})
        self.assertEqual([c["exact"] for c in self.call("check-model", bank["id"])["checked"]], [True])
        rows = self.call("state", "--entity", bank["id"][:8], "--feature", "b")["rows"]
        self.assertEqual([(r["entity"], r["date"], r["values"], r["stamps"]) for r in rows],
                         [("A bank", keys["b"]["as_of"], {"b": 2 / 3}, {"b": keys["b"]["as_of"]})])
        self.assertEqual(self.call("state", "--feature", "none", status=1), {"error": "Not found: no feature none"})
        self.assertEqual(self.call("state", "--from", "2020-01-01", "--to", "2020-12-31")["rows"], [])
        self.assertEqual(self.call("state", "--date", "2020-01-01", "--to", "2020-12-31", status=1),
                         {"error": "give --date or --from/--to, not both"})

    def test_environment_selects_the_default_database(self):
        shared = self.directory / "shared.sqlite"
        run = lambda *arguments, path=str(shared): subprocess.run(
            [sys.executable, "-m", "stated", *arguments], text=True, capture_output=True,
            cwd=self.directory, env={**self.env, "STATED_DB": path})
        self.assertEqual(json.loads(run("init").stdout), {"database": str(shared.resolve())})
        self.assertEqual(json.loads(run("search", "anything").stdout), {"records": []})
        self.assertIn("does not exist", run("--db", "other.sqlite", "search", "x").stderr)
        self.assertEqual(json.loads(run("init", path="").stdout),
                         {"database": str((self.directory / "research.sqlite").resolve())})
