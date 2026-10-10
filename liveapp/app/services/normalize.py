"""Shared normalization helpers.

These mirror the exact rules already used by ``find_easy_sbf.py``,
``achieved_sbf.py`` and ``build_dashboard.py`` so the database rows join and
aggregate identically to the file-based pipeline.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Optional, Sequence

import pandas as pd

BLANK_CODE_VALUES = {"", "NAN", "NONE", "NA", "N/A", "NULL", "<NA>", "NAT"}

COLUMN_ALIASES = {
    "user_code": ("user_code", "usercode"),
    "retailer_code": ("retailer_code", "retailercode"),
    "visit_date": ("visit_date", "vist_date", "visitdate", "vistdate"),
    "dse_code": ("dse_code", "dsecode"),
    "channel_description": ("channel_description", "channeldesc"),
    "no_sale": ("no_sale", "nosale"),
    "easy_sbf": ("easy_sbf", "easy_sbf_list", "easy_sbf_master", "easysbf"),
    "easy_sbf_gap": ("easy_sbf_gap", "easysbfgap"),
}

COLUMN_FALLBACKS = {
    "visit_date": (("visit",), ("vist",), ("date",)),
    "dse_code": (("dse",),),
    "channel_description": (("channel",),),
    "no_sale": (("no", "sale"), ("nosale",)),
    "easy_sbf": (("easy", "sbf"), ("sbf",)),
    "easy_sbf_gap": (("easy", "sbf", "gap"),),
}


def normalize_column_name(name: object) -> str:
    text = str(name).strip()
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    text = re.sub(r"[\s\-./]+", "_", text)
    text = re.sub(r"[^a-zA-Z0-9_]", "", text)
    text = text.lower()
    text = re.sub(r"_+", "_", text)
    return text.strip("_")


def find_column(columns: Sequence[object], standard_key: str) -> Optional[str]:
    normalized = {normalize_column_name(c): c for c in columns}
    for alias in COLUMN_ALIASES.get(standard_key, ()):
        if alias in normalized:
            return normalized[alias]
    for tokens in COLUMN_FALLBACKS.get(standard_key, ()):
        for column in columns:
            if all(token in normalize_column_name(column) for token in tokens):
                return column
    return None


def normalize_code(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if pd.isna(value):
            return ""
        text = str(int(value)) if value.is_integer() else repr(value)
    else:
        text = str(value)
    text = "".join(ch for ch in text if ch.isprintable())
    text = re.sub(r"\s+", " ", text)
    text = text.replace("-", "_")
    text = re.sub(r"[^a-zA-Z0-9_]", "", text)
    text = text.strip("_").upper()
    return "" if text in BLANK_CODE_VALUES else text


def clean_name(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_sbf_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    text = str(value)
    text = re.sub(r"\(\s*\d+\s*\)", "", text)
    text = re.sub(r"[\s_\-]+", " ", text)
    text = re.sub(r"[^a-zA-Z0-9 ]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.upper()


def clean_sbf_readable(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"\(\s*\d+\s*\)", "", text)
    text = re.sub(r"[\s_\-]+", " ", text)
    text = re.sub(r"[^a-zA-Z0-9 ]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_hist_date(value: object) -> Optional[str]:
    """Return an ISO ``YYYY-MM-DD`` string (or None)."""
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def split_sbf(value: object) -> list[str]:
    """Split a comma-separated SBF string into clean, deduped items."""
    if value is None:
        return []
    if isinstance(value, float) and pd.isna(value):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for part in str(value).split(","):
        item = clean_name(part)
        key = item.upper()
        if item and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def to_date(value: object) -> Optional[dt.date]:
    iso = parse_hist_date(value)
    if iso is None:
        return None
    return dt.date.fromisoformat(iso)


def iso_str(value: object) -> Optional[str]:
    return parse_hist_date(value)
