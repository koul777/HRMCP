"""Conservative spelling suggestions derived only from official unit names.

This index is a query aid, not an alias source or a replacement for NCS data.
The search runtime owns its lifetime together with the snapshot lexicon.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable


_WORD = re.compile(r"[a-z가-힣]+")
_MIN_LENGTH = 3
_MAX_LENGTH = 32


def _fold(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _one_edit(left: str, right: str) -> bool:
    """Accept one insertion, deletion, substitution or adjacent transposition."""
    if left == right or abs(len(left) - len(right)) > 1:
        return False
    # Most vocabulary entries differ before the second character. Cheap
    # string checks discard them without constructing a distance matrix.
    if left[0] != right[0]:
        if len(left) == len(right):
            return left[1:] == right[1:] or (
                len(left) > 1
                and left[:2] == right[:2][::-1]
                and left[2:] == right[2:]
            )
        shorter, longer = (left, right) if len(left) < len(right) else (right, left)
        return shorter == longer[1:]
    boundary = min(len(left), len(right))
    index = next((i for i in range(boundary) if left[i] != right[i]), boundary)
    if len(left) == len(right):
        return left[index + 1:] == right[index + 1:] or (
            index + 1 < boundary
            and left[index] == right[index + 1]
            and left[index + 1] == right[index]
            and left[index + 2:] == right[index + 2:]
        )
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    return shorter[index:] == longer[index + 1:]


class UnitNameTypoIndex:
    """Suggest a unique one-edit corpus word or lexical prefix.

    Valid prefixes are left alone. Two-character words, identifiers, multiple
    edits and ambiguous suggestions are deliberately rejected. A shorter
    correction must be a complete source word: truncating to an arbitrary
    compound prefix would make unrelated unknown words appear correct.
    """

    def __init__(self, words: Iterable[str]) -> None:
        self._words = {
            word
            for text in words
            for word in _WORD.findall(_fold(str(text or "")))
            if _MIN_LENGTH <= len(word) <= _MAX_LENGTH
        }
        self._prefixes: dict[int, set[str]] = defaultdict(set)
        for word in self._words:
            for length in range(_MIN_LENGTH, len(word) + 1):
                self._prefixes[length].add(word[:length])

    def suggest(self, token: str) -> list[str]:
        query = _fold(token)
        length = len(query)
        if (
            not _MIN_LENGTH <= length <= _MAX_LENGTH
            or _WORD.fullmatch(query) is None
            or query in self._prefixes.get(length, ())
        ):
            return []
        candidates: set[str] = set()
        for size in (length - 1, length, length + 1):
            for candidate in self._prefixes.get(size, ()):
                if len(candidate) < 4 and candidate not in self._words:
                    continue
                if size < length and candidate not in self._words:
                    continue
                if _one_edit(query, candidate):
                    candidates.add(candidate)
                    if len(candidates) > 1:
                        return []
        return sorted(candidates)
