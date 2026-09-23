"""Dependency-free splitting into exact, nonoverlapping UTF-8 byte ranges."""

import re


def split_ranges(text: str, *, max_chars: int = 2000) -> tuple[tuple[int, int], ...]:
    """Prefer paragraph, line and whitespace breaks in the latter half of a window."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")
    ranges = []
    start_char = start_byte = 0
    while start_char < len(text):
        end_char = min(start_char + max_chars, len(text))
        if end_char < len(text):
            window = text[start_char:end_char]
            for pattern in (r"\r?\n[ \t]*\r?\n", r"\r?\n", r"\s+"):
                boundary = max((match.end() for match in re.finditer(pattern, window)), default=0)
                if boundary >= max(1, max_chars // 2):
                    end_char = start_char + boundary
                    break
        end_byte = start_byte + len(text[start_char:end_char].encode("utf-8"))
        ranges.append((start_byte, end_byte))
        start_char, start_byte = end_char, end_byte
    return tuple(ranges)
