"""A JSON command-line interface to the existing record store."""

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import sys

from .store import RecordStore
from .reranking import OpenRouterReranker
from .retrieval import retrieve


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
    search.add_argument("--kind")
    search.add_argument("--limit", type=int, default=10)
    command("contribute", "contribute", "Create records and links in one transaction; links can name new records by ref.",
            payload="author_id, records [{ref, kind, text, optional attributes and citations}], "
                    "links [{from_id, to_id, relation, optional explanation}]")
    command("link", "create_link", "Connect two records with a named relationship.",
            "from_id", "to_id", "relation", payload="author_id, optional explanation")
    command("get-link", "read_link", "Read a current link.", "link_id")
    command("update-link", "update_link", "Replace a link's endpoints, relation and explanation.",
            "link_id", payload="from_id, to_id, relation, author_id, optional explanation")
    connections = command("connections", "connections",
                          "Read one-hop links, the records they reach and a has_more flag.", "record_id")
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
    command("relations", "relation_usage", "Count links by relation and the record kinds they connect.")
    listing = command("list", "list_records", "List records in creation order; continue with --after LAST_ID.")
    listing.add_argument("--kind")
    listing.add_argument("--author", dest="author_id")
    listing.add_argument("--after", metavar="RECORD_ID")
    listing.add_argument("--limit", type=int, default=100)
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


def main(argv=None):
    options = vars(_parser().parse_args(argv))
    path = Path(options.pop("db"))
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
            if method == "search_documents":
                result = _search_documents(db, options)
            else:
                result = (getattr(db, method)(**options, **payload) if method
                          else {"database": str(path.resolve())})
        print(json.dumps(result, default=asdict, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
