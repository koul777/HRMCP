"""Shared, deterministic normalization for Builder-derived search fields."""

from __future__ import annotations

import unicodedata
from typing import Any


SEARCH_NORMALIZATION_SCHEMA = "ncs_search_normalization_v1"
SEARCH_NORMALIZATION_SOURCE = "builder_derived_from_read_only_source"
SEARCH_NORMALIZATION_FIELDS = {
    "competency_units": {
        "unit_name_raw": "unit_name_search_norm",
        "api_definition": "api_definition_search_norm",
    },
    "competency_elements": {"element_name_raw": "element_name_search_norm"},
    "performance_criteria": {
        "criteria_text_raw": "criteria_text_raw_search_norm",
        "criteria_text_refined": "criteria_text_refined_search_norm",
    },
    "ksa_items": {
        "ksa_text_raw": "ksa_text_raw_search_norm",
        "ksa_text_refined": "ksa_text_refined_search_norm",
    },
    "classifications": {
        name: f"{name}_search_norm"
        for name in ("major_name", "middle_name", "small_name", "sub_name")
    },
    # Builder derives this from alias_text + normalized_query on each row;
    # runtime GROUP_CONCAT preserves the existing per-unit alias aggregation.
    "ncs_query_aliases": {"alias_search_text": "alias_search_norm"},
}
SEARCH_NORMALIZATION_SOURCE_FIELDS = {
    table: tuple(mappings)
    for table, mappings in SEARCH_NORMALIZATION_FIELDS.items()
}
SEARCH_NORMALIZATION_SOURCE_FIELDS["ncs_query_aliases"] = (
    "alias_text",
    "normalized_query",
)
SEARCH_NORMALIZATION_REQUIRED_MANIFEST = {
    "search_normalization_schema": SEARCH_NORMALIZATION_SCHEMA,
    "search_normalization_source": SEARCH_NORMALIZATION_SOURCE,
    "raw_ksa_parity_status": "verified_equal",
}

# V1 remains readable; v2 stores unchanged large text only in the raw field.
# A NULL override means identity, while an empty override is a real normalized
# value (for example punctuation-only input) and must never fall back to raw.
SEARCH_NORMALIZATION_V2_SCHEMA = "ncs_search_normalization_v2"
SEARCH_NORMALIZATION_V2_STORAGE = "hybrid_dense_sparse_override_v2"
SEARCH_NORMALIZATION_V2_OVERRIDES = {
    "competency_elements": {"element_name_raw": "element_name_search_override"},
    "ksa_items": {
        "ksa_text_raw": "ksa_text_raw_search_override",
        "ksa_text_refined": "ksa_text_refined_search_override",
    },
}
SEARCH_NORMALIZATION_V2_FIELDS = {
    table: {
        raw: SEARCH_NORMALIZATION_V2_OVERRIDES.get(table, {}).get(raw, derived)
        for raw, derived in fields.items()
    }
    for table, fields in SEARCH_NORMALIZATION_FIELDS.items()
}
SEARCH_NORMALIZATION_V2_REQUIRED_MANIFEST = {
    **SEARCH_NORMALIZATION_REQUIRED_MANIFEST,
    "search_normalization_schema": SEARCH_NORMALIZATION_V2_SCHEMA,
    "search_normalization_storage": SEARCH_NORMALIZATION_V2_STORAGE,
}


def normalize_search_text(value: Any) -> str:
    """Fold compatibility/case variants and separate punctuation and spaces."""
    text = unicodedata.normalize("NFKC", "" if value is None else str(value))
    separated = "".join(
        " " if char.isspace() or unicodedata.category(char).startswith("P") else char
        for char in text
    )
    return " ".join(separated.split()).casefold()
