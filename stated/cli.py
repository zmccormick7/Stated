"""A JSON command-line interface to the existing record store."""

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import sys

from .context import OpenRouterContextWriter, write_document_context
from .fetch import contact, fetch_page, pdf_text
from .store import RecordStore
from .reranking import OpenRouterReranker
from .answers import check_answer
from .recall import recall
from .retrieval import retrieve


def _where_item(text):
    """KEY=VALUE, with VALUE read as JSON when it is valid JSON and as a string otherwise."""
    key, separator, raw = text.partition("=")
    if not separator or not key:
        raise argparse.ArgumentTypeError("use KEY=VALUE")
    try:
        return key, json.loads(raw)
    except ValueError:
        return key, raw


def _parser():
    parser = argparse.ArgumentParser(prog="stated", description=__doc__)
    parser.add_argument("--db", default=os.environ.get("STATED_DB"),
                        help="Database file (default: $STATED_DB, else research.sqlite; ui also looks in ~/Stated)")
    parser.add_argument("--as-of", default=os.environ.get("STATED_AS_OF") or None, metavar="DATE",
                        help="Read as a reader on this date could have: only sources published by then, and records "
                             "whose evidence or stamp is by then; writes are stamped with it (default: $STATED_AS_OF).")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name, method, description, *arguments, payload=None):
        sub = commands.add_parser(name, help=description, description=description)
        sub.set_defaults(method=method)
        for argument in arguments:
            sub.add_argument(argument)
        if payload:
            sub.add_argument("--input", required=True, metavar="FILE",
                             help=f"JSON object: {payload}. Use - for stdin.")
        return sub

    record = "text, author_id, optional attributes"
    init = command("init", None, "Create or open a research database.")
    init.add_argument("--layer", default="workspace",
                      help="Name of the workspace's own layer when creating the database (default: workspace).")
    command("layers", "layers", "List the workspace's own layer and any attached layers.")
    export = command("export-layer", "export_layer",
                     "Publish the workspace's own layer as a new database file that other workspaces can attach.",
                     "path")
    export.add_argument("--version", required=True, help="Version label recorded in the file, such as 2026.09.30.")
    command("attach-layer", "attach_layer",
            "Attach a published layer file read-only, or update an attached layer to the file's version.", "path")
    command("create", "create", "Create an Entity, Source, Finding, Question, Model or Variable.",
            "kind", payload=record)
    command("get", "read", "Read a current record.", "record_id")
    command("update", "update", "Replace a record's text and attributes in full.",
            "record_id", payload=record)
    command("patch", "patch", "Change text or some top-level attributes, keeping the rest.", "record_id",
            payload="author_id, optional text, attributes (keys to set), remove (keys to delete), citations")
    search = command("search", "search_records", "Search record text; any query word can match.", "query")
    search.add_argument("--full", action="store_true", help="Return complete records instead of previews.")
    search.add_argument("--kind")
    search.add_argument("--limit", type=int, default=10)
    command("contribute", "contribute", "Create records and links in one transaction; links can name new records by ref.",
            payload="author_id, records [{ref, kind, text, optional attributes and citations}], "
                    "links [{from_id, to_id, relation, optional explanation}]")
    command("link", "create_link", "Connect two records with a named relationship.",
            "from_id", "to_id", "relation", payload="author_id, optional explanation")
    command("get-link", "read_link", "Read a current link.", "link_id")
    command("merge", "merge", "Fold a duplicate record into the one kept: move its links, retire it as superseded.",
            "keep_id", "duplicate_id", payload="author_id")
    command("patch-link", "patch_link", "Change some of a link's fields, keeping the rest.", "link_id",
            payload="author_id, optional from_id, to_id, relation, explanation")
    command("update-link", "update_link", "Replace a link's endpoints, relation and explanation.",
            "link_id", payload="from_id, to_id, relation, author_id, optional explanation")
    connections = command("connections", "connections",
                          "Read one-hop links, each with a preview of the record at its other end.", "record_id")
    connections.add_argument("--full", action="store_true", help="Return complete records instead of previews.")
    connections.add_argument("--direction", choices=("both", "in", "out"), default="both")
    connections.add_argument("--relation")
    connections.add_argument("--limit", type=int, default=100)
    walk = command("expand", "expand", "Walk links from seed records, one or two hops, reporting each record's path.")
    walk.add_argument("seed_ids", nargs="+", metavar="SEED_ID")
    walk.add_argument("--depth", type=int, choices=(1, 2), default=1)
    walk.add_argument("--direction", choices=("both", "in", "out"), default="both")
    walk.add_argument("--relation", dest="relations", action="append", help="Follow only this relation; repeatable.")
    walk.add_argument("--limit", type=int, default=100, help="Maximum records reached beyond the seeds.")
    walk.add_argument("--per-record", dest="per_record_limit", type=int, default=50,
                      help="Maximum links followed from any one record.")
    walk.add_argument("--full", action="store_true", help="Return complete records instead of previews.")
    rec = command("recall", "recall", "Assemble connected research for a question: records with paths, cited evidence, "
                  "disagreements and open Questions.", "query")
    rec.add_argument("--seed", dest="seeds", action="append", default=[], metavar="RECORD_ID",
                     help="Start from this record as well as search hits; repeatable.")
    rec.add_argument("--max-records", type=int, default=40)
    rec.add_argument("--max-chars", type=int, default=24000,
                     help="Drop the lowest-ranked records until compact output fits (default 24000).")
    rec.add_argument("--full", action="store_true", help="Return complete records instead of previews.")
    command("relations", "relation_usage", "Count links by relation and the record kinds they connect.")
    check = command("check-model", "check_model",
                    "Evaluate each formula from stored values and compare: a Model's outputs, an Entity's features, "
                    "or one feature Variable against what it depends on.", "model_id")
    check.add_argument("--tolerance", type=float, default=1e-4,
                       help="Relative difference allowed between a stored value and its formula (default 1e-4).")
    scenario = command("evaluate-model", "evaluate_model",
                       "Recompute a model's formulas with some Variables set to other values; nothing is written.",
                       "model_id")
    scenario.add_argument("--set", dest="overrides", action="append", type=_where_item, metavar="KEY=VALUE",
                          help="Hold a Variable at VALUE, a number or a JSON list for a row; repeatable.")
    command("fill-model", "fill_model",
            "Store each formula's exact value where the stored value differs, for a Model, an Entity's features "
            "or one feature Variable; a changed value becomes a new version.", "model_id", payload="author_id")
    state = command("state", "state_rows",
                    "Export feature values (Variables about an Entity) as change points: one row per Entity and "
                    "date on which a feature changed, holding the latest version of every feature by then.")
    state.add_argument("--entity", dest="entity_id", help="Only this Entity.")
    state.add_argument("--feature", dest="features", action="append", metavar="KEY", help="Only this feature; repeatable.")
    state.add_argument("--date", metavar="DATE", help="The state on one date (default: the cutoff, or today).")
    state.add_argument("--from", dest="start", metavar="DATE", help="A row at this date, then one at each change up to --to.")
    state.add_argument("--to", dest="end", metavar="DATE", help="Default: the cutoff, or today.")
    state.add_argument("--limit", type=int, default=1000)
    definition = "author_id, instructions, type (number, count, boolean or category), optional units, min, max, " \
                 "categories (for a category) and formula (for a derived feature)"
    command("define-feature", "define_feature",
            "Define a feature: the instructions an agent follows for one value of a subject as of a date, and the "
            "value's type. New instructions for a defined key name the current definition in replaces.",
            payload="key, " + definition + ", replaces")
    command("update-feature", "update_feature", "Rewrite a feature definition that no value names yet.",
            "feature_id", payload=definition)
    command("feature", "read_feature", "Read a feature definition by ID, or the current one for a key.", "feature")
    command("schedule-feature", "schedule_feature",
            "Set the dates a value of a feature is due for each subject, or clear them with every null.", "feature",
            payload="every (day, weekday, week, month, quarter or null), start (a date)")
    todo = command("pending", "pending_work",
                   "List the scheduled dates on which a subject has neither a value of the feature nor a check.", "feature")
    todo.add_argument("--subject", dest="subject_ids", action="append", default=[], metavar="ENTITY_ID",
                      help="A subject to list; repeatable.")
    todo.add_argument("--subjects-file", metavar="FILE", help="A file of Entity IDs, one per line.")
    todo.add_argument("--from", dest="start", metavar="DATE", help="Default: the schedule's start.")
    todo.add_argument("--to", dest="end", metavar="DATE", help="Default: the cutoff, or today.")
    todo.add_argument("--limit", type=int, default=1000)
    runner = command("run-features", "run_features",
                     "Run the pending dates of features: at each subject's date, a fresh agent session at that cutoff, "
                     "the web off, for every feature due, writing values or confirming the latest; features with no "
                     "new document about the subject are confirmed without one. Each subject's dates in order.")
    runner.add_argument("features", nargs="+", metavar="FEATURE")
    runner.add_argument("--every-date", dest="skip_unchanged", action="store_false",
                        help="Run a session even when no new document about the subject has been published.")
    runner.add_argument("--subject", dest="subject_ids", action="append", default=[], metavar="ENTITY_ID")
    runner.add_argument("--subjects-file", metavar="FILE", help="A file of Entity IDs, one per line.")
    runner.add_argument("--from", dest="start", metavar="DATE")
    runner.add_argument("--to", dest="end", metavar="DATE")
    runner.add_argument("--workers", type=int, default=1, help="Subjects run at once (default 1).")
    runner.add_argument("--max-sessions", type=int, help="Stop after this many sessions.")
    runner.add_argument("--session-minutes", type=float, default=20, help="Each session is stopped after this long.")
    runner.add_argument("--author", dest="author_id", help="author_id for what the sessions write "
                                                           "(default: feature-run/HARNESS).")
    runner.add_argument("--harness", default="claude-code", choices=["claude-code", "codex"])
    runner.add_argument("--model", help="With --harness codex: the model to pass to codex exec.")
    runner.add_argument("--effort", dest="reasoning_effort", help="With --harness codex: the model_reasoning_effort.")
    deriving = command("derive-features", "derive_features",
                       "Compute each subject's derived features at every date an input changed, in date order; "
                       "run-features does this after each subject's sessions.")
    deriving.add_argument("--subject", dest="subject_ids", action="append", default=[], metavar="ENTITY_ID")
    deriving.add_argument("--subjects-file", metavar="FILE", help="A file of Entity IDs, one per line.")
    deriving.add_argument("--from", dest="start", metavar="DATE")
    deriving.add_argument("--to", dest="end", metavar="DATE")
    deriving.add_argument("--author", dest="author_id", default="derive-features",
                          help="author_id for the values written (default: derive-features).")
    table = command("feature-table", "feature_table",
                    "Write the daily feature table as CSV: one row per subject and date, each feature's latest value "
                    "dated by then, the date it was dated and the latest date it was read.")
    table.add_argument("--feature", dest="features", action="append", metavar="KEY",
                       help="A feature by key or definition ID; repeatable (default: every current definition).")
    table.add_argument("--subject", dest="subject_ids", action="append", metavar="ENTITY_ID")
    table.add_argument("--subjects-file", metavar="FILE", help="A file of Entity IDs, one per line.")
    table.add_argument("--from", dest="start", metavar="DATE", required=True)
    table.add_argument("--to", dest="end", metavar="DATE", help="Default: the cutoff, or today.")
    table.add_argument("--every", choices=("day", "weekday"), default="day")
    table.add_argument("--out", required=True, metavar="FILE", help="The CSV file to write.")
    command("confirm", "confirm_value",
            "Record that a feature's latest value about its subject still holds on this date, instead of "
            "writing it again.", "value_id", payload="author_id")
    features = command("features", "list_features", "List the current feature definitions by key.")
    features.add_argument("--replaced", action="store_true", help="Include definitions that were replaced.")
    command("check-answer", "check_answer",
            "Check an answer document against the graph: its sources, quotes and every charted figure.",
            payload="an answer document: title, body, optional sources and blocks")
    ui = command("ui", "ui", "Run the local answer UI for this workspace until interrupted.")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--harness", default="claude-code", choices=["claude-code", "codex"],
                    help="The coding agent that answers (default: claude-code).")
    ui.add_argument("--model", help="With --harness codex: the model to pass to codex exec.")
    ui.add_argument("--effort", dest="reasoning_effort", help="With --harness codex: the model_reasoning_effort to set.")
    ui.add_argument("--base", metavar="FILE", help="A published base layer file to attach, or to update to.")
    ui.add_argument("--contact", metavar="TEXT", help="Your name and email, kept beside the database and sent only to "
                                                      "SEC.gov, which requires one to fetch filings.")
    ui.add_argument("--no-open", dest="open_browser", action="store_false", help="Do not open the page in a browser.")
    listing = command("list", "list_records", "List records in creation order; continue with --after LAST_ID.")
    listing.add_argument("--full", action="store_true", help="Return complete records instead of previews.")
    listing.add_argument("--kind")
    listing.add_argument("--author", dest="author_id", help="Last editor.")
    listing.add_argument("--created-by", help="Creator, which edits never change.")
    listing.add_argument("--since", metavar="UTC_TIME", help="Created or edited at or after this UTC time or date.")
    listing.add_argument("--where", action="append", type=_where_item, metavar="KEY=VALUE",
                         help="Match a top-level attribute exactly; repeatable. VALUE is read as JSON when valid.")
    listing.add_argument("--after", metavar="RECORD_ID")
    listing.add_argument("--limit", type=int, default=100)
    links = command("links", "list_links", "List links in creation order; continue with --after LAST_LINK_ID.")
    links.add_argument("--full", action="store_true", help="Return links without previews of their ends.")
    links.add_argument("--relation")
    links.add_argument("--author", dest="author_id", help="Last editor.")
    links.add_argument("--created-by", help="Creator, which edits never change.")
    links.add_argument("--since", metavar="UTC_TIME", help="Created or edited at or after this UTC time or date.")
    links.add_argument("--after", metavar="LINK_ID")
    links.add_argument("--limit", type=int, default=100)
    ingest = command("ingest", "ingest_source",
                     "Ingest an extracted UTF-8 text file without normalization, or the text of a web page.",
                     payload="description, author_id, optional attributes")
    ingest.add_argument("source_file", nargs="?", help="Path to the extracted text file, or to a PDF")
    ingest.add_argument("--url", help="Fetch this HTML or plain-text page or PDF and ingest its text instead of a file; "
                                      "records url and retrieved_at in the Source's attributes.")
    source = command("source-text", "read_source_text", "Read source text as a JSON string; offsets are UTF-8 bytes.", "source_id")
    source.add_argument("--start-byte", type=int, default=0)
    source.add_argument("--end-byte", type=int)
    locate = command("locate", "locate_source_text",
                     "Find exact UTF-8 byte ranges of a quotation for citations; use - to read the quote "
                     "from stdin.", "source_id", "quote")
    locate.add_argument("--limit", type=int, default=10)
    cited = command("cited-by", "cited_by", "Records citing any part of a source byte range, in source order.", "source_id")
    cited.add_argument("--start-byte", type=int, default=0)
    cited.add_argument("--end-byte", type=int)
    cited.add_argument("--limit", type=int, default=100)
    chunk = command("chunk", "chunk_source", "Replace passages and return their count; enables document search.", "source_id")
    chunk.add_argument("--max-chars", type=int, default=2000)
    documents = command("search-documents", "search_documents", "Find relevant source excerpts for a research question.", "query")
    documents.add_argument("--source-id", dest="source_ids", action="append")
    documents.add_argument("--limit", dest="max_segments", type=int, default=argparse.SUPPRESS,
                           help="Maximum excerpts to return (default: 5)")
    documents.add_argument("--max-tokens", dest="max_total_tokens", type=int, default=argparse.SUPPRESS,
                           help="Total excerpt budget (default: 6000)")
    command("set-context", "set_document_context", "Set a document's title and summary, separately from evidence.",
            "source_id", payload="title, summary, author_id, optional provenance")
    command("get-context", "read_document_context", "Read document context, or null if absent.", "source_id")
    return parser


def _write_table(table, out):
    """Write a FeatureTable as CSV: nulls empty, booleans true or false, lists as JSON."""
    import csv
    cell = lambda v: "" if v is None else ("true" if v is True else "false" if v is False
                                           else json.dumps(v) if isinstance(v, list) else v)
    with open(out, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(table.columns)
        writer.writerows([cell(v) for v in row] for row in table.rows)
    return {"path": str(Path(out).resolve()), "rows": len(table.rows), "columns": list(table.columns)}


def _run_features(path, options, cutoff):
    """Run a feature's pending sessions, printing each session's result to stderr as it ends and the run's
    summary to stdout."""
    from .feature_runs import derive_features, run_features
    from .ui.harness import ClaudeCode, Codex
    from .ui.server import install_skills
    try:
        if cutoff:
            raise ValueError(f"{options['command']} runs without a cutoff; it works at each date in turn")
        if not path.is_file():
            raise ValueError(f"Database does not exist: {path}. Use init to create it.")
        subjects = list(options["subject_ids"])
        if options["subjects_file"]:
            subjects += [line.strip() for line in Path(options["subjects_file"]).read_text(encoding="utf-8").splitlines()
                         if line.strip() and not line.startswith("#")]
        if options["method"] == "derive_features":
            made = derive_features(path, subjects, author_id=options["author_id"], start=options["start"],
                                   end=options["end"])
            print(json.dumps({"derived": [asdict(item) for item in made]}))
            return 0
        if options["harness"] != "codex" and (options["model"] or options["reasoning_effort"]):
            raise ValueError("--model and --effort apply to the codex harness")
        harness = (Codex(model=options["model"], reasoning_effort=options["reasoning_effort"])
                   if options["harness"] == "codex" else ClaudeCode())
        install_skills(harness)
        report = lambda result: print(json.dumps(asdict(result)), file=sys.stderr, flush=True)
        result = run_features(path, options["features"], subjects, harness=harness, workers=options["workers"],
                              start=options["start"], end=options["end"], author_id=options["author_id"],
                              max_sessions=options["max_sessions"], session_minutes=options["session_minutes"],
                              skip_unchanged=options["skip_unchanged"], on_result=report)
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        message = f"Not found: {error.args[0]}" if isinstance(error, KeyError) and error.args else str(error)
        print(json.dumps({"error": message}), file=sys.stderr)
        return 1
    print(json.dumps({"features": list(result.keys), "remaining": result.remaining,
                      "sessions": sum(s.session for s in result.sessions),
                      "confirmed_without_session": sum(not s.session for s in result.sessions),
                      "done": sum(s.done for s in result.sessions),
                      "failed": [asdict(s) for s in result.sessions if not s.done],
                      "derived": len([d for d in result.derived if d.id]),
                      "not_derived": [asdict(d) for d in result.derived if d.problem]}))
    return 0


def _add_document_context(db, source, author_id):
    """Give a newly ingested Source a model-written title and summary when OpenRouter is configured.

    The Source is already committed, so a failed call only warns; context is a retrieval aid, not evidence.
    """
    if not os.environ.get("OPENROUTER_API_KEY", "").strip():
        return
    try:
        writer = OpenRouterContextWriter(os.environ.get("STATED_CONTEXT_MODEL", "openai/gpt-6-luna"),
                                         reasoning_effort=os.environ.get("STATED_CONTEXT_REASONING", "low"))
        write_document_context(db, source.id, writer=writer, author_id=author_id)
    except (ValueError, RuntimeError) as error:
        print(json.dumps({"warning": f"Source {source.id} was ingested without document context: {error}"}),
              file=sys.stderr)


def _search_documents(db, options):
    try:
        import tiktoken
    except ImportError:
        raise RuntimeError("Document search requires tiktoken; install with: python -m pip install 'tiktoken>=0.12,<1'") from None
    encoding = tiktoken.get_encoding(os.environ.get("STATED_TOKEN_ENCODING", "cl100k_base"))
    reranker = OpenRouterReranker(os.environ.get("STATED_RERANKER_MODEL", "voyageai/rerank-2.5"))
    result = retrieve(db, reranker=reranker,
                      count_tokens=lambda text: len(encoding.encode(text, disallowed_special=())), **options)
    return [{**asdict(excerpt.segment), "token_count": excerpt.token_count} for excerpt in result.excerpts]


PREVIEW_ATTRIBUTES = ("basis", "status", "key", "value")


def _preview(record, *, edits=True):
    """ID, kind, the start of the text, a few telling attributes, the number of citations and, with
    edits, who last edited the record (and created it, when that differs), when, and its layer."""
    preview = {"id": record.id, "kind": record.kind,
               "text": record.text if len(record.text) <= 200 else record.text[:199] + "…"}
    preview |= {name: record.attributes[name] for name in PREVIEW_ATTRIBUTES if name in record.attributes}
    if isinstance(citations := record.attributes.get("citations"), list) and citations:
        preview["citations"] = len(citations)
    if edits:
        preview["author_id"] = record.author_id
        if record.created_by != record.author_id:
            preview["created_by"] = record.created_by
        preview["updated_at"] = record.updated_at
        preview["as_of"] = record.as_of
        preview["layer"] = record.layer
    return preview


def _compact(db, method, result, options):
    """Previews in place of full records for search, list and expand; linked records previewed on each link."""
    if method == "search_records":
        return [_preview(record) for record in result]
    if method == "list_records":
        return {"records": [_preview(record) for record in result.records], "has_more": result.has_more}
    if method == "connections":  # The other end of each link sits beside it, so no second lookup is needed.
        other = lambda link: link.from_id if link.to_id == options["record_id"] else link.to_id
        return {"links": [asdict(link) | {"other": _preview(result.records[other(link)], edits=False)}
                          for link in result.links], "has_more": result.has_more}
    if method == "list_links":
        return {"links": [asdict(link) | {"from": _preview(db.read(link.from_id), edits=False),
                                          "to": _preview(db.read(link.to_id), edits=False)}
                          for link in result.links], "has_more": result.has_more}
    return {"reached": [_preview(r.record) | {"depth": r.depth, "path": [
                {"id": link.id, "from_id": link.from_id, "to_id": link.to_id, "relation": link.relation}
                for link in r.path]} for r in result.reached],
            "capped": list(result.capped), "has_more": result.has_more}


# Every result is a JSON object; collections sit under a named key so parsers can rely on one shape.
LIST_KEYS = {"search_records": "records", "search_documents": "excerpts", "locate_source_text": "matches",
             "state_rows": "rows", "list_features": "features",
             "relation_usage": "relations", "layers": "layers"}


def _shape(method, result):
    if method in LIST_KEYS and isinstance(result, (list, tuple)):
        return {LIST_KEYS[method]: list(result)}
    if method == "connections" and not isinstance(result, dict):  # --full: connected records as a list.
        return {"links": result.links, "has_more": result.has_more, "records": list(result.records.values())}
    return result


def _clip(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _recall(db, options):
    """Compact by default, so the whole bundle fits in an agent's view; --full returns everything."""
    full, max_chars = options.pop("full"), options.pop("max_chars")
    if max_chars < 2000:
        raise ValueError("max_chars must be at least 2000")
    result = recall(db, options.pop("query"), **options)
    if full:
        return result
    shown = {r.record.id for r in result.records}
    records = []
    for r in result.records:
        item = _preview(r.record, edits=False)  # Edit details would crowd out records.
        if r.path:  # Reached by walking: shorter text, and how it was reached.
            item["text"] = _clip(r.record.text, 100)
            # via names a record in this bundle, so a short prefix is enough to match it or pass it back.
            item |= {"via": r.entry_id[:8], "path": [link.relation for link in r.path]}
        else:
            item["entry_channels"] = list(r.channels)
        records.append(item)
    evidence, cited = [], set()
    for item in result.evidence:  # One quote per record keeps the bundle small.
        if not cited.issuperset(item.record_ids):
            cited.update(item.record_ids)
            evidence.append({"source_id": item.source_id, "start_byte": item.start_byte, "end_byte": item.end_byte,
                             "quote": _clip(item.quote, 240), "record_ids": [i[:8] for i in item.record_ids]})
    frontier = [db.read(record_id) for record_id in result.frontier[:10]]
    bundle = {"records": records,
            "evidence": evidence,
            "issues": {"links": [{"from_id": l.from_id, "to_id": l.to_id, "relation": l.relation,
                                  "explanation": _clip(l.explanation, 200)} for l in result.issue_links],
                       "open_questions": [{"id": q.id, "text": _clip(q.text, 120), "status": q.attributes.get("status", "open")}
                                          for q in result.open_questions if q.id not in shown]},
            "frontier": [{"id": f.id, "kind": f.kind, "text": _clip(f.text, 80)} for f in frontier],
            "coverage": result.coverage | {"evidence_omitted": len(result.evidence) - len(evidence),
                                           "frontier_omitted": result.coverage["frontier_omitted"]
                                           + max(0, len(result.frontier) - 10), "dropped_for_size": 0}}
    # A hard size cap, so the bundle stays readable however large the graph grows.
    while len(json.dumps(bundle, default=asdict)) > max_chars and len(bundle["records"]) > 1:
        dropped = bundle["records"].pop()["id"]
        kept = {r["id"] for r in bundle["records"]}
        bundle["evidence"] = [e for e in bundle["evidence"] if set(e["record_ids"]) != {dropped[:8]}]
        bundle["issues"]["links"] = [l for l in bundle["issues"]["links"] if {l["from_id"], l["to_id"]} & kept]
        bundle["coverage"]["dropped_for_size"] += 1
        bundle["coverage"]["truncated"] = True
    return bundle


def main(argv=None):
    options = vars(_parser().parse_args(argv))
    given, as_of = options.pop("db"), options.pop("as_of")
    fixed = os.environ.get("STATED_AS_OF") or None
    if options["method"] == "ui":
        from .ui.server import serve
        try:
            return serve(given, harness_name=options["harness"], port=options["port"], model=options["model"],
                         reasoning_effort=options["reasoning_effort"], base=options["base"],
                         contact=options["contact"], open_browser=options["open_browser"])
        except (OSError, ValueError, sqlite3.Error) as error:
            print(json.dumps({"error": str(error)}), file=sys.stderr)
            return 1
    path = Path(given or "research.sqlite")
    if options["method"] in ("run_features", "derive_features"):
        return _run_features(path, options, fixed or as_of)
    for name in ("where", "overrides"):
        if options.get(name) is not None:
            options[name] = dict(options[name])
    method = options.pop("method")
    options.pop("command")
    try:
        if str(path) == ":memory:":
            raise ValueError("The CLI requires a persistent database file")
        if fixed and as_of != fixed:  # A session's cutoff is set by whoever started it, not chosen per command.
            raise ValueError(f"the cutoff is fixed at {fixed} by STATED_AS_OF")
        if method is not None and not path.is_file():
            raise ValueError(f"Database does not exist: {path}. Use init to create it.")
        payload = {}
        if filename := options.pop("input", None):
            raw = sys.stdin.read() if filename == "-" else Path(filename).read_text(encoding="utf-8")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("--input must contain a JSON object")
        if method == "ingest_source":
            filename, url = options.pop("source_file"), options.pop("url")
            if (filename is None) == (url is None):
                raise ValueError("give ingest a text file or --url, not both")
            if url is not None and as_of:
                raise ValueError("ingest --url is off at a cutoff: a fetched page carries today's date; "
                                 "ingest a file with its publication date in attributes.published")
            if url is None:
                data = Path(filename).read_bytes()  # A PDF is read for its text; anything else is the text itself.
                options["document_text"] = pdf_text(data) if data.startswith(b"%PDF-") else data.decode("utf-8")
            else:
                page = fetch_page(url, contact=contact(path))
                options["document_text"] = page.text
                if not isinstance(payload.get("attributes") or {}, dict):
                    raise ValueError("attributes must be a JSON object")
                payload["attributes"] = {"url": page.url, "retrieved_at": page.retrieved_at} | (payload.get("attributes") or {})
        if (subjects_file := options.pop("subjects_file", None)) is not None:
            lines = Path(subjects_file).read_text(encoding="utf-8").splitlines()
            options["subject_ids"] = (options["subject_ids"] or []) + [
                line.strip() for line in lines if line.strip() and not line.startswith("#")]
        if "date" in options:
            if (date := options.pop("date")) is not None:
                if options.get("start") or options.get("end"):
                    raise ValueError("give --date or --from/--to, not both")
                options["start"] = options["end"] = date
        if options.get("quote") == "-":  # Drop the final newline a heredoc adds.
            options["quote"] = sys.stdin.read().removesuffix("\n")
        if options.keys() & payload.keys():
            raise ValueError("JSON input must not repeat arguments supplied by the command")
        out = options.pop("out", None)
        with RecordStore(path, layer=options.pop("layer", "workspace"), as_of=as_of) as db:
            # Positional IDs may be unique prefixes of 8+ characters, like the short IDs in reports.
            for name in ("record_id", "source_id", "from_id", "to_id", "link_id", "model_id", "keep_id", "duplicate_id",
                         "entity_id"):
                if options.get(name) is not None:
                    options[name] = db.resolve_id(options[name], link=name == "link_id")
            if "seed_ids" in options:
                options["seed_ids"] = [db.resolve_id(seed) for seed in options["seed_ids"]]
            if method == "recall":
                result = _recall(db, options)
            elif method == "check_answer":
                result = check_answer(db, payload)
            elif method == "search_documents":
                result = _search_documents(db, options)
            else:
                full = options.pop("full", True)
                result = (getattr(db, method)(**options, **payload) if method
                          else {"database": str(path.resolve())})
                if not full:
                    result = _compact(db, method, result, options)
                if method == "ingest_source":
                    _add_document_context(db, result, payload["author_id"])
                if method == "feature_table":
                    result = _write_table(result, out)
        print(json.dumps(_shape(method, result), default=asdict, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error) as error:
        message = f"Not found: {error.args[0]}" if isinstance(error, KeyError) and error.args else str(error)
        print(json.dumps({"error": message}), file=sys.stderr)
        return 1
