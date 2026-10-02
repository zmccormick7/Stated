"""Answer documents: what an agent returns for a question, checked against the graph before it is shown.

An answer is a JSON object with a one-line `title`, a `body` paragraph, numbered `sources` and optional
`blocks` (text, quote, chart). `[n]` in body or text cites `sources[n - 1]`. Every figure a chart draws
names the record it comes from: a Variable supplies its stored value, and any other record must contain
the figure as written, so a chart cannot show a number the graph does not hold.
"""

from dataclasses import dataclass
import re

from .recall import RETIRED_STATUSES

MAX_BLOCKS = 6
_KEYS = {
    "answer": ({"title", "body"}, {"sources", "blocks"}),
    "source": ({"record"}, {"quote"}),
    "text": ({"type", "text"}, set()),
    "quote": ({"type", "source"}, set()),
    "chart": ({"type", "unit", "rows"}, {"caption", "whole", "rest_label", "lead"}),
    "row": ({"label", "parts"}, set()),
    "part": ({"label", "record"}, {"value", "as_written", "scale", "period"}),
}


@dataclass(frozen=True)
class AnswerCheck:
    ok: bool
    problems: tuple[str, ...]
    answer: dict | None  # The document with full record IDs and every figure's value, when ok.


def check_answer(db, answer: object) -> AnswerCheck:
    """Check an answer document against the graph; problems are reported, not raised."""
    problems: list[str] = []
    if not _shape(answer, "answer", "answer", problems):
        return AnswerCheck(False, tuple(problems), None)
    title, body = answer["title"], answer["body"]
    if not _text(title) or "\n" in title or len(title) > 120:
        problems.append("title must be one nonblank line of at most 120 characters")
    if not _text(body):
        problems.append("body must be a nonblank string")
    sources = answer.get("sources", [])
    blocks = answer.get("blocks", [])
    if not isinstance(sources, list) or not isinstance(blocks, list):
        return AnswerCheck(False, tuple(problems + ["sources and blocks must be lists"]), None)
    if len(blocks) > MAX_BLOCKS:
        problems.append(f"an answer has at most {MAX_BLOCKS} blocks")
    resolved_sources = [_source(db, source, f"sources[{i + 1}]", problems) for i, source in enumerate(sources)]
    used: set[int] = set()
    _markers(body, "body", len(sources), used, problems)
    resolved_blocks = []
    for i, block in enumerate(blocks):
        where = f"blocks[{i + 1}]"
        kind = block.get("type") if isinstance(block, dict) else None
        if kind not in ("text", "quote", "chart"):
            problems.append(f"{where}: type must be text, quote or chart")
        elif _shape(block, kind, where, problems):
            resolved_blocks.append(_BLOCKS[kind](db, block, where, sources, used, problems))
    if sum(isinstance(b, dict) and b.get("lead") is True for b in blocks) > 1:
        problems.append("at most one block may be the lead")
    for unused in sorted(set(range(1, len(sources) + 1)) - used):
        problems.append(f"sources[{unused}] is never cited; cite it with [{unused}] or remove it")
    if problems:
        return AnswerCheck(False, tuple(problems), None)
    return AnswerCheck(True, (), {"title": title, "body": body, "sources": resolved_sources, "blocks": resolved_blocks})


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _shape(value, name, where, problems):
    required, optional = _KEYS[name]
    if not isinstance(value, dict):
        problems.append(f"{where} must be an object")
        return False
    missing, unknown = sorted(required - value.keys()), sorted(value.keys() - required - optional)
    if missing:
        problems.append(f"{where} lacks {', '.join(missing)}")
    if unknown:
        problems.append(f"{where} has unknown keys: {', '.join(unknown)}")
    return not missing and not unknown


def _record(db, value, where, problems):
    try:
        record = db.read(db.resolve_id(value))
    except (KeyError, ValueError, TypeError):
        problems.append(f"{where}: no record {value!r}")
        return None
    if record.attributes.get("status") in RETIRED_STATUSES:
        problems.append(f"{where}: record {record.id[:8]} is {record.attributes['status']}")
        return None
    return record


def _evidence(db, record):
    """The texts a quote or figure may come from: the record's own text and the passages it cites."""
    texts = [record.text]
    for citation in record.attributes.get("citations") or []:
        if not isinstance(citation, dict):
            continue
        quote = citation.get("quote")
        if not isinstance(quote, str):
            try:
                quote = db.read_source_text(citation.get("source_id"), start_byte=citation.get("start_byte"),
                                            end_byte=citation.get("end_byte"))
            except (KeyError, ValueError):
                continue
        texts.append(quote)
    return texts


def _source(db, source, where, problems):
    if not _shape(source, "source", where, problems) or (record := _record(db, source["record"], where, problems)) is None:
        return None
    resolved = {"record": record.id, "kind": record.kind}
    if "quote" in source:
        quote = source["quote"]
        if not _text(quote) or not any(quote in text for text in _evidence(db, record)):
            problems.append(f"{where}: the quote is not in record {record.id[:8]} or the passages it cites")
        resolved["quote"] = quote
    return resolved


def _markers(text, where, count, used, problems):
    for marker in re.findall(r"\[(\d+)\]", text if isinstance(text, str) else ""):
        if not 1 <= int(marker) <= count:
            problems.append(f"{where} cites [{marker}], which is not among the sources")
        used.add(int(marker))


def _text_block(db, block, where, sources, used, problems):
    if not _text(block["text"]):
        problems.append(f"{where}: text must be a nonblank string")
    _markers(block["text"], where, len(sources), used, problems)
    return dict(block)


def _quote_block(db, block, where, sources, used, problems):
    number = block["source"]
    if type(number) is not int or not 1 <= number <= len(sources):
        problems.append(f"{where}: source must be the number of a source")
    else:
        used.add(number)
        if not isinstance(sources[number - 1], dict) or "quote" not in sources[number - 1]:
            problems.append(f"{where}: sources[{number}] has no quote to show")
    return dict(block)


def _chart_block(db, block, where, sources, used, problems):
    whole = block.get("whole")
    if not _text(block["unit"]) or not isinstance(block["rows"], list) or not block["rows"]:
        problems.append(f"{where}: a chart needs a unit and at least one row")
        return None
    if not isinstance(block.get("lead", False), bool):
        problems.append(f"{where}: lead must be true or false")
    if whole is not None and not _is_number(whole):
        problems.append(f"{where}: whole must be a number")
        whole = None
    rows = []
    for r, row in enumerate(block["rows"]):
        at = f"{where}.rows[{r + 1}]"
        if not _shape(row, "row", at, problems) or not isinstance(row["parts"], list) or not row["parts"]:
            continue
        parts = []
        for p, part in enumerate(row["parts"]):
            there = f"{at}.parts[{p + 1}]"
            if _shape(part, "part", there, problems) and (figure := _figure(db, part, there, problems)) is not None:
                parts.append({"label": part["label"], "record": figure[0], "value": figure[1]})
        if whole is not None and len(parts) == len(row["parts"]) and sum(x["value"] for x in parts) > whole * 1.005:
            problems.append(f"{at}: the parts add up to more than the whole")
        rows.append({"label": row["label"], "parts": parts})
    return {**block, "rows": rows}


def _figure(db, part, where, problems):
    """The record ID and value of one charted figure, or None after reporting why it cannot be drawn."""
    record = _record(db, part["record"], where, problems)
    if record is None:
        return None
    if record.kind == "Variable":
        if part.keys() & {"value", "as_written", "scale"}:
            problems.append(f"{where}: a Variable's value is read from the graph; give only record and period")
            return None
        value, periods = record.attributes.get("value"), record.attributes.get("periods")
        if isinstance(value, list):
            if not isinstance(periods, list) or part.get("period") not in periods:
                problems.append(f"{where}: period must be one of {periods}")
                return None
            value = value[periods.index(part["period"])]
        elif "period" in part:
            problems.append(f"{where}: Variable {record.id[:8]} has a single value, not periods")
            return None
        if not _is_number(value):
            problems.append(f"{where}: Variable {record.id[:8]} has no number to draw")
            return None
        return record.id, value
    value, written, scale = part.get("value"), part.get("as_written"), part.get("scale", 1)
    if not _is_number(value) or not _text(written) or not _is_number(scale) or "period" in part:
        problems.append(f"{where}: a figure from a {record.kind} needs value and as_written, and optionally scale")
        return None
    pattern = r"(?<![\d.,])" + re.escape(written.strip()) + r"(?![\d]|[.,]\d)"
    if not any(re.search(pattern, text) for text in _evidence(db, record)):
        problems.append(f'{where}: "{written}" does not appear in record {record.id[:8]} or the passages it cites')
        return None
    parsed = _parse(written)
    if parsed is None or abs(parsed * scale - value) > 0.005 * abs(value):
        problems.append(f'{where}: "{written}" times scale {scale} is not the value {value}')
        return None
    return record.id, value


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse(written):
    """The number in a figure as written, such as "$193,479", "22%" or "(1.5)"; None when it is not one."""
    text = written.strip()
    negative = text.startswith(("-", "−", "("))
    try:
        number = float(re.sub(r"[\s$€£%,()−-]", "", text))
    except ValueError:
        return None
    return -number if negative else number


_BLOCKS = {"text": _text_block, "quote": _quote_block, "chart": _chart_block}
