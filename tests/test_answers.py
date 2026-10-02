from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from premisedb import RecordStore, check_answer


class AnswerCheckTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = RecordStore(Path(directory.name) / "research.sqlite")
        self.addCleanup(self.db.close)
        text = ("Sales to one direct customer represented 22% of total revenue and sales to another represented "
                "14%. Compute revenue was $193,479 million. The prior year's loss was (1.5) billion.")
        source = self.db.ingest_source(description="PowerFrame 10-K", document_text=text, author_id="a")
        self.source = source.id
        self.finding = self.db.contribute(author_id="a", records=[{
            "ref": "f", "kind": "Finding", "text": "PowerFrame's two largest customers were 22% and 14% of revenue.",
            "attributes": {"basis": "disclosed"},
            "citations": [{"source_id": source.id, "quote": "Compute revenue was $193,479 million."},
                          {"source_id": source.id, "quote": "The prior year's loss was (1.5) billion."}]}]).records["f"].id
        self.scalar = self.db.create("Variable", text="Revenue", author_id="a",
                                     attributes={"key": "revenue", "value": 485.0}).id
        self.row = self.db.create("Variable", text="Capex", author_id="a", attributes={
            "key": "capex", "value": [10, 12.5], "periods": ["2026", "2027"]}).id

    def answer(self, **changes):
        return {"title": "Two customers were 36% of revenue.", "body": "One was 22% and another 14%. [1]",
                "sources": [{"record": self.finding[:8], "quote": "Compute revenue was $193,479 million."}],
                "blocks": [{"type": "chart", "unit": "% of revenue", "whole": 100, "rest_label": "Everyone else",
                            "lead": True, "rows": [{"label": "Fiscal 2026", "parts": [
                                {"label": "Largest", "record": self.finding, "value": 22, "as_written": "22%"},
                                {"label": "Second", "record": self.finding, "value": 14, "as_written": "14%"}]}]},
                           {"type": "quote", "source": 1}]} | changes

    def part(self, **part):
        return self.answer(blocks=[{"type": "chart", "unit": "$B", "rows": [
            {"label": "Row", "parts": [{"label": "Part"} | part]}]}])

    def test_a_grounded_answer_is_resolved(self):
        result = check_answer(self.db, self.answer())
        self.assertEqual((result.ok, result.problems), (True, ()))
        self.assertEqual(result.answer["sources"], [{"record": self.finding, "kind": "Finding",
                                                     "quote": "Compute revenue was $193,479 million."}])
        self.assertEqual(result.answer["blocks"][0]["rows"][0]["parts"], [
            {"label": "Largest", "record": self.finding, "value": 22},
            {"label": "Second", "record": self.finding, "value": 14}])
        self.assertTrue(check_answer(self.db, {"title": "No.", "body": "The graph holds nothing on this."}).ok)

    def test_figures_come_from_the_graph(self):
        for part, value in (({"record": self.scalar}, 485.0), ({"record": self.row, "period": "2027"}, 12.5),
                            # A figure in a cited passage, rescaled; and an accounting negative.
                            ({"record": self.finding, "value": 193.5, "as_written": "$193,479", "scale": 0.001}, 193.5),
                            ({"record": self.finding, "value": -1.5, "as_written": "(1.5)"}, -1.5)):
            with self.subTest(part=part):
                result = check_answer(self.db, self.part(**part))
                self.assertEqual(result.problems, ())
                self.assertEqual(result.answer["blocks"][0]["rows"][0]["parts"][0]["value"], value)
        for part, message in (
                ({"record": self.finding, "value": 24, "as_written": "24%"}, '"24%" does not appear in record'),
                ({"record": self.finding, "value": 2, "as_written": "2%"}, "does not appear"),  # Not inside 22%.
                ({"record": self.finding, "value": 23, "as_written": "22%"}, "is not the value 23"),
                ({"record": self.finding, "value": 22}, "needs value and as_written"),
                ({"record": self.scalar, "value": 485.0}, "read from the graph"),
                ({"record": self.row}, "period must be one of"),
                ({"record": self.scalar, "period": "2027"}, "single value"),
                ({"record": "missing-record"}, "no record 'missing-record'")):
            with self.subTest(part=part):
                result = check_answer(self.db, self.part(**part))
                self.assertFalse(result.ok)
                self.assertIsNone(result.answer)
                self.assertIn(message, result.problems[0])

    def test_a_lever_names_an_input_and_an_output_of_a_model(self):
        model = self.db.create("Model", text="Revenue model", author_id="a").id
        growth = self.db.create("Variable", text="Growth", author_id="a", attributes={
            "key": "growth", "role": "input", "value": 0.4, "low": 0.2, "high": 0.6, "units": "fraction y/y"}).id
        revenue = self.db.create("Variable", text="Revenue", author_id="a", attributes={
            "key": "revenue", "role": "output", "value": 541.3, "formula": "386.6 * (1 + growth)", "units": "USD bn"}).id
        self.db.create("Variable", text="Row", author_id="a", attributes={"key": "row", "value": [1, 2], "periods": ["a", "b"]})
        for variable in (growth, revenue, self.db.search_records("Row")[0].id):
            self.db.create_link(variable, model, "in_model", author_id="a")
        lever = {"type": "lever", "model": model[:8], "input": "growth", "output": "revenue", "lead": True,
                 "input_label": "Supply growth"}
        result = check_answer(self.db, self.answer(blocks=[lever]))
        self.assertEqual(result.problems, ())
        self.assertEqual(result.answer["blocks"][0], {
            "type": "lever", "model": model, "lead": True,
            "input": {"key": "growth", "record": growth, "label": "Supply growth", "value": 0.4, "units": "fraction y/y",
                      "low": 0.2, "high": 0.6},
            "output": {"key": "revenue", "record": revenue, "label": "revenue", "value": 541.3, "units": "USD bn"}})
        spanned = check_answer(self.db, self.answer(blocks=[lever | {"range": [0, 1]}])).answer["blocks"][0]["input"]
        self.assertEqual((spanned["low"], spanned["high"]), (0, 1))
        defaulted = check_answer(self.db, self.answer(blocks=[lever | {"input": "revenue", "output": "growth"}]))
        span = defaulted.answer["blocks"][0]["input"]
        self.assertEqual((round(span["low"], 6), round(span["high"], 6)), (270.65, 811.95))  # Half to one and a half.
        for change, message in (({"model": self.finding}, "no Model"), ({"model": "nope"}, "no Model"),
                                ({"input": "tariff"}, "no current Variable with key 'tariff'"),
                                ({"input": "row"}, "must have a single numeric value"),
                                ({"output": "growth"}, "input and output must differ"),
                                ({"range": [1, 0]}, "range must be"), ({"range": [0.5, 0.6]}, "outside the range"),
                                ({"input_label": ""}, "input_label must be"), ({"lead": "yes"}, "lead must be true or false")):
            with self.subTest(change=change):
                result = check_answer(self.db, self.answer(blocks=[lever | change]))
                self.assertFalse(result.ok)
                self.assertIn(message, result.problems[0])

    def test_problems_are_reported_together(self):
        self.db.patch(self.scalar, author_id="a", attributes={"status": "retracted"})
        answer = self.answer(title="Two lines\nof title", body="See [1] and [3].", caveats="none")
        self.assertEqual(check_answer(self.db, answer).problems, ("answer has unknown keys: caveats",))
        del answer["caveats"]
        answer["sources"] += [{"record": self.finding, "quote": "not in the record"}, {"record": self.scalar}]
        answer["blocks"] = [{"type": "text", "text": "More. [4]"}, {"type": "quote", "source": 3},
                            {"type": "table"}, {"type": "text"}] + answer["blocks"][:1] * 2
        answer["blocks"][4] = answer["blocks"][4] | {"whole": 30}
        self.assertEqual(check_answer(self.db, answer).problems, (
            "title must be one nonblank line of at most 120 characters",
            f"sources[2]: the quote is not in record {self.finding[:8]} or the passages it cites",
            f"sources[3]: record {self.scalar[:8]} is retracted",
            "blocks[1] cites [4], which is not among the sources",
            "blocks[2]: sources[3] has no quote to show",
            "blocks[3]: type must be text, quote, chart or lever",
            "blocks[4] lacks text",
            "blocks[5].rows[1]: the parts add up to more than the whole",
            "at most one block may be the lead",
            "sources[2] is never cited; cite it with [2] or remove it"))
        self.assertEqual(check_answer(self.db, ["title"]).problems, ("answer must be an object",))
        self.assertIn("at most 6 blocks", check_answer(self.db, self.answer(blocks=[
            {"type": "text", "text": "x"}] * 7, sources=[], body="b")).problems[0])


if __name__ == "__main__":
    unittest.main()
