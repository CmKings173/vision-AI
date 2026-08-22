"""Named business-field extraction profiles for specific label families."""

from __future__ import annotations

import re

from .fields import FieldExtractor


DGX_SPARK_LABEL_FIELDS = (
    "customer_part_number",
    "so_number",
    "our_part_number",
    "quantity",
    "net_weight",
    "gross_weight",
    "carton_number",
)


DGX_SPARK_LABEL_PATTERNS = {
    "customer_part_number": re.compile(
        r"\b(?:CUSTOMER\s+PART(?:\s+NUMBER|\s+NO\.?)?|CUSTOMER\s+P/N|CPN)"
        r"\s*[:#=\-]?\s*([A-Z0-9][A-Z0-9._/\-]{2,})",
        re.IGNORECASE,
    ),
    "so_number": re.compile(
        r"\b(?:S\s*/\s*O\s*NO\.?|SALES\s+ORDER(?:\s+NO\.?)?)"
        r"\s*[:#=\-]?\s*([A-Z0-9][A-Z0-9._/\-]{2,})",
        re.IGNORECASE,
    ),
    "our_part_number": re.compile(
        r"\b(?:OUR\s+PART(?:\s+NUMBER|\s+NO\.?)?|OUR\s+P/N)"
        r"\s*[:#=\-]?\s*([A-Z0-9][A-Z0-9._/\-]{2,})",
        re.IGNORECASE,
    ),
    "quantity": re.compile(
        r"\b(?:QTY|QUANTITY)\s*[:#=\-]?\s*([0-9]+(?:[.,][0-9]+)?)",
        re.IGNORECASE,
    ),
    "net_weight": re.compile(
        r"\b(?:N\.?\s*W\.?|NET\s+WEIGHT)\s*[:#=\-]?\s*"
        r"([0-9]+(?:[.,][0-9]+)?(?:\s*(?:KG|KGS|G|LB|LBS))?)",
        re.IGNORECASE,
    ),
    "gross_weight": re.compile(
        r"\b(?:G\.?\s*W\.?|GROSS\s+WEIGHT)\s*[:#=\-]?\s*"
        r"([0-9]+(?:[.,][0-9]+)?(?:\s*(?:KG|KGS|G|LB|LBS))?)",
        re.IGNORECASE,
    ),
    "carton_number": re.compile(
        r"\b(?:CARTON\s*(?:NO\.?|NUMBER|#)|CTN\s*(?:NO\.?|#))"
        r"\s*[:#=\-]?\s*([A-Z0-9][A-Z0-9._/\-]{0,})",
        re.IGNORECASE,
    ),
}


def build_extractor(profile: str = "default") -> FieldExtractor:
    """Build a deterministic extractor for a named label profile."""

    normalized = profile.strip().lower().replace("-", "_")
    if normalized in {"dgx_spark_label", "dgx_spark"}:
        return FieldExtractor(
            fields=DGX_SPARK_LABEL_FIELDS,
            patterns=DGX_SPARK_LABEL_PATTERNS,
        )
    if normalized == "default":
        return FieldExtractor()
    raise ValueError(f"unsupported extraction profile: {profile}")
