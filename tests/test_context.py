from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from premisedb import ContextWriter, OpenRouterContextWriter, RecordStore, write_document_context
from premisedb.cli import main


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
        self.assertIn("generic form or section name", title_prompt)  # Names the issuer and period instead.
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

    def test_cli_ingest_adds_context_without_being_asked(self):
        text_file = self.path.parent / "note.txt"
        text_file.write_text("PowerFrame Q2 2026 results. Backlog rose.", encoding="utf-8")
        environment = {k: v for k, v in os.environ.items() if not k.startswith("PREMISEDB_CONTEXT_")}  # Defaults.
        def ingest(description, key, response):
            stdout, stderr = StringIO(), StringIO()
            with patch.dict(os.environ, {**environment, "OPENROUTER_API_KEY": key}, clear=True), \
                    patch("sys.stdin", StringIO(json.dumps({"description": description, "author_id": "agent"}))), \
                    patch("premisedb.context.urlopen", **response) as request, \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["--db", str(self.path), "ingest", str(text_file), "--input", "-"])
            return code, json.loads(stdout.getvalue()), stderr.getvalue(), request
        answers = [BytesIO(json.dumps({"choices": [{"message": {"content": text}}]}).encode())
                   for text in ("PowerFrame Q2 2026 results", "This document is about: PowerFrame's Q2 2026 results.")]
        code, source, stderr, request = ingest("Note", "key", {"side_effect": answers})
        self.assertEqual((code, stderr, request.call_count), (0, "", 2))
        context = self.db.read_document_context(source["id"])
        self.assertEqual((context.title, context.author_id, context.provenance["model"]),
                         ("PowerFrame Q2 2026 results", "agent", "openai/gpt-6-luna"))
        text_file.write_text("A second note.", encoding="utf-8")
        code, source, stderr, _ = ingest("Second", "key", {"side_effect": HTTPError("u", 429, "", {}, BytesIO())})
        self.assertEqual(code, 0)  # The Source is kept; context is only a retrieval aid.
        self.assertIn("ingested without document context: OpenRouter HTTP 429", json.loads(stderr)["warning"])
        self.assertIsNone(self.db.read_document_context(source["id"]))
        text_file.write_text("A third note.", encoding="utf-8")
        code, source, stderr, request = ingest("Third", "", {})
        self.assertEqual((code, stderr, request.call_count), (0, "", 0))  # No key: no calls, no warning.
        self.assertIsNone(self.db.read_document_context(source["id"]))

if __name__ == "__main__":
    unittest.main()
