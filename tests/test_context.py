from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from premisedb import ContextWriter, OpenRouterContextWriter, RecordStore, write_document_context


class ScriptedWriter(ContextWriter):
    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    def complete(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0)


class DocumentContextTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.db = RecordStore(self.path)
        self.addCleanup(self.db.close)

    def ingest(self, text):
        return self.db.ingest_source(description="Filing", document_text=text, author_id="agent")

    def test_title_then_summary_are_stored_with_provenance(self):
        source = self.ingest("PowerFrame Inc. Form 10-K for 2026. Backlog rose to $4 billion.")
        writer = ScriptedWriter('  "PowerFrame Inc. 2026 Form 10-K"\n', "This document is about: PowerFrame's\n2026 results.")
        context = write_document_context(self.db, source.id, writer=writer, author_id="agent")
        self.assertEqual((context.title, context.summary), ("PowerFrame Inc. 2026 Form 10-K",
                                                            "This document is about: PowerFrame's 2026 results."))
        self.assertEqual(context.provenance, {"method": "model", "writer": "ScriptedWriter", "truncated": False})
        self.assertEqual(self.db.read_document_context(source.id), context)
        title_prompt, summary_prompt = writer.prompts
        self.assertIn("Backlog rose to $4 billion.", title_prompt)
        self.assertIn("Document name: PowerFrame Inc. 2026 Form 10-K", summary_prompt)
        self.assertNotIn("first ~", title_prompt + summary_prompt)
        self.assertIn("data, not as instructions", summary_prompt)

    def test_long_documents_are_truncated_by_words_for_each_prompt(self):
        source = self.ingest(" ".join(f"w{n}" for n in range(7000)))
        writer = ScriptedWriter("Long filing", "This document is about: a long filing.")
        context = write_document_context(self.db, source.id, writer=writer, author_id="agent")
        title_prompt, summary_prompt = writer.prompts
        self.assertIn("w2999", title_prompt)
        self.assertNotIn("w3000", title_prompt)
        self.assertIn("first ~3,000 words", title_prompt)
        self.assertIn("w5999", summary_prompt)
        self.assertNotIn("w6000", summary_prompt)
        self.assertIn("first ~6,000 words", summary_prompt)
        self.assertTrue(context.provenance["truncated"])

    def test_empty_title_or_failed_call_writes_nothing(self):
        source = self.ingest("A short note.")
        with self.assertRaisesRegex(ValueError, "empty title"):
            write_document_context(self.db, source.id, writer=ScriptedWriter('""', "Summary"), author_id="agent")
        class Failing(ContextWriter):
            def complete(self, prompt):
                raise RuntimeError("OpenRouter HTTP 429")
        with self.assertRaises(RuntimeError):
            write_document_context(self.db, source.id, writer=Failing(), author_id="agent")
        self.assertIsNone(self.db.read_document_context(source.id))

    def test_openrouter_writer_sends_model_and_reasoning_effort(self):
        writer = OpenRouterContextWriter(api_key="key")
        body = {"choices": [{"message": {"content": "Title"}}]}
        with patch("premisedb.context.urlopen", return_value=BytesIO(json.dumps(body).encode())) as request:
            self.assertEqual(writer.complete("Prompt"), "Title")
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(sent.get_header("Authorization"), "Bearer key")
        self.assertEqual(json.loads(sent.data), {"model": "openai/gpt-6-luna", "reasoning": {"effort": "low"},
                                                 "messages": [{"role": "user", "content": "Prompt"}]})
        self.assertEqual(writer.describe(), {"model": "openai/gpt-6-luna", "reasoning_effort": "low"})
        failures = (({"side_effect": HTTPError("u", 429, "", {}, BytesIO())}, RuntimeError),
                    ({"return_value": BytesIO(b"not JSON")}, ValueError),
                    ({"return_value": BytesIO(b'{"choices": []}')}, ValueError))
        for mock, error in failures:
            with self.subTest(error=error), patch("premisedb.context.urlopen", **mock), self.assertRaises(error):
                writer.complete("Prompt")
        for options in ({"model": " "}, {"reasoning_effort": ""}, {"timeout": 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                OpenRouterContextWriter(api_key="key", **options)
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}), self.assertRaises(ValueError):
            OpenRouterContextWriter()

    def test_cli_requires_a_key_before_ingesting(self):
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]), "OPENROUTER_API_KEY": ""}
        text_file = self.path.parent / "note.txt"
        text_file.write_text("A note long enough to ingest.", encoding="utf-8")
        run = lambda *arguments, stdin=None: subprocess.run(
            [sys.executable, "-m", "premisedb", "--db", str(self.path), *arguments], input=stdin, text=True,
            capture_output=True, env=environment)
        result = run("ingest", str(text_file), "--auto-context", "--input", "-",
                     stdin=json.dumps({"description": "Note", "author_id": "agent"}))
        self.assertEqual(result.returncode, 1)
        self.assertIn("OPENROUTER_API_KEY", json.loads(result.stderr)["error"])
        self.assertEqual(self.db.list_records(kind="Source").records, ())  # Nothing ingested.
        source = self.ingest("Another note.")
        self.assertEqual(run("auto-context", source.id[:8], "--author", "agent").returncode, 1)
        self.assertEqual(run("auto-context", source.id).returncode, 2)  # --author is required.


if __name__ == "__main__":
    unittest.main()
