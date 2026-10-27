"""Fetch a web page's or PDF's text for ingestion, so an agent needs no general download tool."""

from dataclasses import dataclass
from datetime import UTC, datetime
import gzip
from html.parser import HTMLParser
import io
import logging
import os
from pathlib import Path
import re
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

MAX_BYTES = 50_000_000
_SKIPPED = {"script", "style", "noscript", "template", "ix:header"}
_BLOCKS = {"p", "div", "br", "tr", "li", "table", "section", "article", "ul", "ol", "hr", "pre", "blockquote",
           "h1", "h2", "h3", "h4", "h5", "h6", "title"}
_CELLS = {"td", "th"}


@dataclass(frozen=True)
class Page:
    url: str  # After redirects.
    text: str
    retrieved_at: str  # UTC.


class _Text(HTMLParser):
    """The text a reader sees: blocks on their own lines, table cells apart, scripts and hidden headers dropped."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self._skipping = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIPPED:
            self._skipping += 1
        elif tag in _BLOCKS:
            self.parts.append("\n")
        elif tag in _CELLS:
            self.parts.append("\t")

    def handle_endtag(self, tag):
        if tag in _SKIPPED:
            self._skipping = max(0, self._skipping - 1)
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skipping:
            self.parts.append(re.sub(r"\s+", " ", data))  # A line break in the markup is only a space.


def html_text(html: str) -> str:
    """An HTML document's visible text, one block per line, cells of a table row separated by ' | '."""
    parser = _Text()
    parser.feed(html)
    parser.close()
    lines = []
    for line in "".join(parser.parts).replace("\xa0", " ").split("\n"):
        cells = [" ".join(cell.split()) for cell in line.split("\t")]
        if line := " | ".join(cell for cell in cells if cell):
            lines.append(line)
    return "\n".join(lines) + "\n"


def pdf_text(data: bytes) -> str:
    """A PDF's text, page by page with a form feed between pages. A scanned PDF has none; that needs OCR,
    which is not done here."""
    try:
        from pypdf import PdfReader
        from pypdf.errors import PyPdfError
    except ImportError:
        raise ValueError('reading a PDF needs the pypdf package: pip install "stated[cli]"') from None
    logging.getLogger("pypdf").setLevel(logging.ERROR)  # Its warnings about odd files are not ours to print.
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("the PDF is protected by a password")
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except (PyPdfError, OSError, KeyError, IndexError, TypeError) as error:
        raise ValueError(f"the PDF could not be read: {error}") from None
    if not any(pages):
        raise ValueError("the PDF has no text; a scanned document needs OCR, which is not supported")
    return "\n\f".join(pages) + "\n"


def contact(db_path: str | Path | None = None) -> str | None:
    """Who is asking, for sites that require it: $STATED_CONTACT, else contact.txt beside the database."""
    value = os.environ.get("STATED_CONTACT", "").strip()
    if not value and db_path is not None:
        file = Path(db_path).with_name("contact.txt")
        value = file.read_text(encoding="utf-8").strip() if file.is_file() else ""
    return value or None


def fetch_page(url: str, *, contact: str | None = None, open_url=urlopen) -> Page:
    """Download an HTML or plain-text page or a PDF and return its text. SEC.gov refuses requests that do not say
    who is asking, so it needs `contact` (a name and email), which is sent to SEC.gov and nowhere else."""
    if os.environ.get("STATED_FETCH", "").strip() == "0":
        raise ValueError("fetching from the web is off for this question: it was asked at an effort that works "
                         "from the graph alone")
    parts = urlparse(url if isinstance(url, str) else "")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("url must be an http or https address")
    agent = "Stated/0.0.1"
    if parts.hostname == "sec.gov" or parts.hostname.endswith(".sec.gov"):
        if not contact:
            raise ValueError('SEC.gov requires a contact name and email with each request. Set one once with: '
                             'stated ui --contact "Your Name you@example.com" (or set STATED_CONTACT). '
                             "It is sent to SEC.gov only.")
        agent = contact
    try:
        with open_url(Request(url, headers={"User-Agent": agent, "Accept-Encoding": "gzip"}), timeout=60) as response:
            body = response.read(MAX_BYTES + 1)
            kind = (response.headers.get_content_type() or "").lower()
            charset = response.headers.get_content_charset() or "utf-8"
            encoding, final = response.headers.get("Content-Encoding", ""), response.geturl()
    except (URLError, TimeoutError) as error:  # HTTPError is a URLError.
        raise ValueError(f"could not fetch {url}: {error}") from None
    if len(body) > MAX_BYTES:
        raise ValueError(f"{url} is larger than {MAX_BYTES // 1_000_000} MB")
    if encoding.lower() == "gzip":
        body = gzip.decompress(body)
    if body.startswith(b"%PDF-"):  # Whatever the server calls it.
        try:
            text = pdf_text(body)
        except ValueError as error:
            raise ValueError(f"{url}: {error}") from None
        return Page(final, text, datetime.now(UTC).isoformat(timespec="seconds"))
    if kind not in ("text/html", "application/xhtml+xml", "text/plain"):
        raise ValueError(f"only HTML, plain-text and PDF documents can be fetched; {url} is {kind or 'of an unknown type'}")
    try:
        text = body.decode(charset)
    except (LookupError, UnicodeDecodeError):
        text = body.decode("utf-8", errors="replace")
    if kind != "text/plain":
        text = html_text(text)
    if not text.strip():
        raise ValueError(f"{url} has no text")
    return Page(final, text, datetime.now(UTC).isoformat(timespec="seconds"))
