"""Compact candidate index for the existing left-boundary search predicate.

An exact match of a normalized needle at a lexical boundary necessarily has
the same first two or three characters. Hex-encoded prefixes make those keys
ordinary ASCII FTS tokens, including punctuation and non-Latin characters.
The caller must still apply the original SQL predicate and rank order.
"""
from __future__ import annotations

from typing import Any

PREFIX_FTS_SCHEMA = "ncs_lexical_prefix_fts_v1"
PREFIX_FTS_BOUNDARY_POLICY = "ascii_alnum_underscore_hangul_syllable_superset_v1"
PREFIX_FTS_LENGTHS = (2, 3)
PREFIX_FTS_REQUIRED_MANIFEST = {
    "lexical_prefix_fts_schema": PREFIX_FTS_SCHEMA,
    "lexical_prefix_fts_boundary_policy": PREFIX_FTS_BOUNDARY_POLICY,
    "lexical_prefix_fts_lengths": "2,3",
}
PREFIX_FTS_TABLES = {"ksa": "ksa_prefix_fts", "criteria": "criteria_prefix_fts"}


def _known_word_character(character: str) -> bool:
    """A fixed subset of the runtime's Unicode L/N/M/underscore characters.

    Treat every other character as a possible boundary. Extra candidates are
    harmless; the original predicate rejects them. This avoids depending on
    identical Unicode category versions in Builder and the serving runtime.
    """
    code = ord(character)
    return (0xAC00 <= code <= 0xD7A3 or 48 <= code <= 57 or 65 <= code <= 90
            or 97 <= code <= 122 or code == 95)


def prefix_fts_term(value: Any) -> str | None:
    text = str(value or "")
    if len(text) < PREFIX_FTS_LENGTHS[0]:
        return None
    return "p" + text[:PREFIX_FTS_LENGTHS[-1]].encode("utf-8").hex()


def prefix_fts_document(*normalized_fields: str | None) -> str:
    """Encode a superset of all normalized field starts accepted by search."""
    tokens: set[str] = set()
    for value in normalized_fields:
        text = value or ""
        previous_word = False
        for index, character in enumerate(text):
            if not previous_word:
                for width in PREFIX_FTS_LENGTHS:
                    if index + width <= len(text):
                        tokens.add("p" + text[index:index + width].encode("utf-8").hex())
            previous_word = _known_word_character(character)
    return " ".join(sorted(tokens))
