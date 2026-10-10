"""Conservative, corpus-backed recovery of single-edit unit-name typos."""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable


def name_key(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def one_edit_apart(left: str, right: str) -> bool:
    """One insertion, deletion, substitution, or adjacent transposition."""
    if left == right or abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        changed = [i for i, (a, b) in enumerate(zip(left, right)) if a != b]
        return len(changed) == 1 or (
            len(changed) == 2
            and changed[1] == changed[0] + 1
            and left[changed[0]] == right[changed[1]]
            and left[changed[1]] == right[changed[0]]
        )
    short, long = sorted((left, right), key=len)
    index = next((i for i, (a, b) in enumerate(zip(short, long)) if a != b), len(short))
    return short[index:] == long[index + 1:]


class UnitNameSpellingIndex:
    """A bounded deletion index of official names, never definitions or codes.

    Ambiguous neighbours are rejected. Two-syllable names are excluded because
    a single edit changes too much of their meaning. Exact names remain exact.
    """

    def __init__(self, names: Iterable[str]) -> None:
        self.names: dict[str, str] = {}
        for name in names:
            key = name_key(name)
            if 3 <= len(key) <= 40 and re.fullmatch(r"[가-힣]+", key):
                self.names.setdefault(key, name)
        self.deletions: dict[str, set[str]] = defaultdict(set)
        for key in self.names:
            for i in range(len(key)):
                self.deletions[key[:i] + key[i + 1:]].add(key)

    def correction(self, query: str) -> str | None:
        key = name_key(query)
        if (
            not 3 <= len(key) <= 40
            or not re.fullmatch(r"[가-힣]+", key)
            or key in self.names
        ):
            return None
        candidates = set(self.deletions.get(key, ()))
        for i in range(len(key)):
            deleted = key[:i] + key[i + 1:]
            if deleted in self.names:
                candidates.add(deleted)
            candidates.update(self.deletions.get(deleted, ()))
        for i in range(len(key) - 1):
            swapped = key[:i] + key[i + 1] + key[i] + key[i + 2:]
            if swapped in self.names:
                candidates.add(swapped)
        matches = [candidate for candidate in candidates if one_edit_apart(key, candidate)]
        if len(matches) != 1:
            return None
        return self.names[matches[0]]
