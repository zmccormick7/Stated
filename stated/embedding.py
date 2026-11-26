"""Passage embeddings: one vector a passage, for finding passages by meaning.

A vector is the passage's own text embedded by one model, DIMENSIONS numbers long, stored as one signed byte a
number (the number times SCALE, rounded). Only the text is embedded, never model-written context, so a vector
depends on nothing but fixed source text: it is never stale and carries nothing a reader at an earlier date could
not see. On 250 searches replayed from feature sessions, vectors this short found what full ones did.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
import json
import math
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .store import RecordStore

DIMENSIONS = 512
SCALE = 400  # A unit vector's numbers are small: none seen above 0.26, and a byte holds up to 127 / 400.


def pack(vector: Sequence[float]) -> bytes:
    """A vector as stored: one signed byte a number."""
    if len(vector) != DIMENSIONS or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector):
        raise ValueError(f"Expected {DIMENSIONS} finite numbers per vector")
    return bytes(max(-127, min(127, round(x * SCALE))) & 0xFF for x in vector)


class Embedder(ABC):
    model: str  # Names the space the vectors lie in; a graph holds vectors of one space.

    @abstractmethod
    def _embed(self, texts: tuple[str, ...]) -> Sequence[Sequence[float]]:
        """Return one unit-length vector of DIMENSIONS numbers per text, in input order."""

    def embed(self, texts: Sequence[str]) -> tuple[bytes, ...]:
        """One stored vector per text, in input order."""
        texts = tuple(texts)
        if any(not isinstance(text, str) or not text for text in texts):
            raise ValueError("texts must be nonempty strings")
        if not texts:
            return ()
        vectors = tuple(self._embed(texts))
        if len(vectors) != len(texts):
            raise ValueError("Expected one vector per text")
        return tuple(pack(vector) for vector in vectors)


class OpenAIEmbedder(Embedder):
    """OpenAI's embedding models, which shorten a vector to the length asked for."""

    def __init__(self, model: str = "text-embedding-3-small", *, api_key: str | None = None,
                 timeout: float = 60, batch_size: int = 96):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a nonblank string")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        key = os.environ.get("OPENAI_API_KEY") if api_key is None else api_key
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Set OPENAI_API_KEY or supply api_key")
        self.model, self.timeout, self.batch_size = model, timeout, batch_size
        self._api_key = key

    def _embed(self, texts: tuple[str, ...]) -> Sequence[Sequence[float]]:
        vectors = []
        for start in range(0, len(texts), self.batch_size):
            some = texts[start:start + self.batch_size]
            request = Request("https://api.openai.com/v1/embeddings",
                              json.dumps({"model": self.model, "input": some, "dimensions": DIMENSIONS}).encode(),
                              headers={"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json"})
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    rows = json.load(response)["data"]
                found = {row["index"]: row["embedding"] for row in rows}
                vectors += [found[index] for index in range(len(some))]
            except HTTPError as error:
                error.close()
                raise RuntimeError(f"OpenAI HTTP {error.code}") from None
            except (URLError, OSError):
                raise RuntimeError("OpenAI connection failed or timed out") from None
            except (KeyError, TypeError, ValueError, UnicodeError):
                raise ValueError("OpenAI returned an incomplete or malformed embedding response") from None
        return vectors


def embed_source(db: RecordStore, source_id: str, *, embedder: Embedder) -> int:
    """Embed each of a Source's passages and store the vectors; return how many."""
    body = db.read_source_text(source_id).encode("utf-8")
    passages = []
    while page := db.read_passages(source_id, start_ordinal=len(passages)):
        passages += page
    vectors = embedder.embed([body[p.start_byte:p.end_byte].decode("utf-8") for p in passages])
    return db.set_passage_vectors(source_id, vectors, model=embedder.model)
