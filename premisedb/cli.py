"""A JSON command-line interface to the existing record store."""

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import sys

from .context import OpenRouterContextWriter, write_document_context
from .store import RecordStore
from .reranking import OpenRouterReranker
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
    parser = argparse.ArgumentParser(prog="premisedb", description=__doc__)
    parser.add_argument("--db", default=os.environ.get("PREMISEDB_DB") or "research.sqlite",
                        help="Database file (default: $PREMISEDB_DB, else research.sqlite)")
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
    command("init", None, "Create or open a research database.")
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
    ingest = command("ingest", "ingest_source", "Ingest an extracted UTF-8 text file without normalization.",
                     payload="description, author_id, optional attributes")
    ingest.add_argument("source_file", help="Path to the extracted text file")
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


def _add_document_context(db, source, author_id):
    """Give a newly ingested Source a model-written title and summary when OpenRouter is configured.

    The Source is already committed, so a failed call only warns; context is a retrieval aid, not evidence.
    """
    if not os.environ.get("OPENROUTER_API_KEY", "").strip():
        return
    try:
        writer = OpenRouterContextWriter(os.environ.get("PREMISEDB_CONTEXT_MODEL", "openai/gpt-6-luna"),
                                         reasoning_effort=os.environ.get("PREMISEDB_CONTEXT_REASONING", "low"))
        write_document_context(db, source.id, writer=writer, author_id=author_id)
    except (ValueError, RuntimeError) as error:
        print(json.dumps({"warning": f"Source {source.id} was ingested without document context: {error}"}),
              file=sys.stderr)


def _search_documents(db, options):
    try:
        import tiktoken
    except ImportError:
        raise RuntimeError("Document search requires tiktoken; install with: python -m pip install 'tiktoken>=0.12,<1'") from None
    encoding = tiktoken.get_encoding(os.environ.get("PREMISEDB_TOKEN_ENCODING", "cl100k_base"))
    reranker = OpenRouterReranker(os.environ.get("PREMISEDB_RERANKER_MODEL", "voyageai/rerank-2.5"))
    result = retrieve(db, reranker=reranker,
                      count_tokens=lambda text: len(encoding.encode(text, disallowed_special=())), **options)
    return [{**asdict(excerpt.segment), "token_count": excerpt.token_count} for excerpt in result.excerpts]


PREVIEW_ATTRIBUTES = ("basis", "status", "key", "value")


def _preview(record, *, edits=True):
    """ID, kind, the start of the text, a few telling attributes, the number of citations and, with
    edits, who last edited the record (and created it, when that differs) and when."""
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
             "relation_usage": "relations"}


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
    path = Path(options.pop("db"))
    if options.get("where") is not None:
        options["where"] = dict(options["where"])
    method = options.pop("method")
    options.pop("command")
    try:
        if str(path) == ":memory:":
            raise ValueError("The CLI requires a persistent database file")
        if method is not None and not path.is_file():
            raise ValueError(f"Database does not exist: {path}. Use init to create it.")
        payload = {}
        if filename := options.pop("input", None):
            raw = sys.stdin.read() if filename == "-" else Path(filename).read_text(encoding="utf-8")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("--input must contain a JSON object")
        if filename := options.pop("source_file", None):
            options["document_text"] = Path(filename).read_bytes().decode("utf-8")
        if options.get("quote") == "-":  # Drop the final newline a heredoc adds.
            options["quote"] = sys.stdin.read().removesuffix("\n")
        if options.keys() & payload.keys():
            raise ValueError("JSON input must not repeat arguments supplied by the command")
        with RecordStore(path) as db:
            # Positional IDs may be unique prefixes of 8+ characters, like the short IDs in reports.
            for name in ("record_id", "source_id", "from_id", "to_id", "link_id"):
                if name in options:
                    options[name] = db.resolve_id(options[name], link=name == "link_id")
            if "seed_ids" in options:
                options["seed_ids"] = [db.resolve_id(seed) for seed in options["seed_ids"]]
            if method == "recall":
                result = _recall(db, options)
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
        print(json.dumps(_shape(method, result), default=asdict, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error) as error:
        message = f"Not found: {error.args[0]}" if isinstance(error, KeyError) and error.args else str(error)
        print(json.dumps({"error": message}), file=sys.stderr)
        return 1
