"""
Read and normalize the user's NCBI metadata workbook into canonical sample
records, applying the organism preset's column aliases and ISO/INSDC
normalization. Also performs the metadata-level deduplication (duplicate rows
and -original vs -repeat pairs) before any NCBI lookups.

Canonical sample fields (all strings; "" when absent):
  sample_id bioproject biosample sra_accession organism host
  isolation_source collection_date geo_loc_name region serotype
plus `row` (1-based source row) and `excluded` / `exclude_reason` once dedup runs.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

import presets

CANONICAL_FIELDS = [
    "sample_id", "bioproject", "biosample", "sra_accession", "organism",
    "host", "isolation_source", "collection_date", "geo_loc_name", "region",
    "serotype",
]

# Suffixes the lab appends to a base isolate name; -repeat* wins over -original*.
_SUFFIX_RE = re.compile(r"-(original|repeat|tile)\d*", re.IGNORECASE)


def base_isolate(sample_id: str) -> str:
    """Strip -original / -repeat / -tile suffixes to get the base isolate name."""
    return _SUFFIX_RE.sub("", str(sample_id or "")).strip()


def is_repeat(sample_id: str) -> bool:
    return bool(re.search(r"-repeat", str(sample_id or ""), re.IGNORECASE))


# ---------------------------------------------------------------------------
# ISO 8601 date normalization
# ---------------------------------------------------------------------------
def normalize_collection_date(value: Any) -> Tuple[str, Optional[str]]:
    """Return (iso_string, error). Accepts year-only, YYYY-MM, full dates, and
    common spellings; emits ISO 8601 (YYYY, YYYY-MM, or YYYY-MM-DD) which is
    what NCBI's collection_date requires."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "", "missing collection_date"
    if isinstance(value, (datetime, date, pd.Timestamp)):
        d = value
        # A bare year imported as Jan 1 should stay a year; we can't tell, so
        # keep the full date — Excel cells carrying a real date are full dates.
        return d.strftime("%Y-%m-%d"), None
    s = str(value).strip()
    if not s:
        return "", "missing collection_date"
    if re.fullmatch(r"\d{4}", s):
        return s, None
    if re.fullmatch(r"\d{4}-\d{2}", s):
        return s, None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s, None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%d-%b-%y", "%Y/%m/%d", "%b-%Y", "%B %Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d"), None
        except ValueError:
            continue
    return s, f"unrecognized date format: {s!r} (expected ISO 8601 YYYY-MM-DD)"


def normalize_geo_loc_name(geo: str, region: str, default_country: str) -> Tuple[str, Optional[str]]:
    """Build an INSDC geo_loc_name 'Country:Region'. If only a region/state is
    given, prefix the preset's default country. Returns (value, error)."""
    geo = (geo or "").strip()
    region = (region or "").strip()
    if geo and ":" in geo:
        return geo, None
    if geo and region and geo != region:
        return f"{geo}:{region}", None
    if geo and not region:
        # A lone token: treat as a country if it has no obvious region marker.
        return geo, None
    if region and default_country:
        return f"{default_country}:{region}", None
    if region:
        return region, "geo_loc_name has a region but no country; INSDC wants Country:Region"
    return "", "missing geo_loc_name"


# ---------------------------------------------------------------------------
# Workbook reading
# ---------------------------------------------------------------------------
def read_metadata(xlsx_path: Path, preset: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Read the workbook into canonical sample dicts. Returns (samples, warnings)."""
    warnings: List[str] = []
    df = pd.read_excel(xlsx_path)
    alias_index = presets.build_alias_index(preset)
    # Map each source column to its canonical field (first wins on collision).
    col_to_canon: Dict[str, str] = {}
    for col in df.columns:
        canon = alias_index.get(presets.normalize_header(col))
        if canon and canon not in col_to_canon.values():
            col_to_canon[col] = canon

    found = set(col_to_canon.values())
    if "sample_id" not in found:
        raise ValueError(
            "Metadata sheet has no sample-id column. Expected one of: "
            f"{preset.get('column_aliases', {}).get('sample_id')}. Found columns: {list(df.columns)}"
        )

    default_country = (preset.get("genbank") or {}).get("default_country", "")
    samples: List[Dict[str, Any]] = []
    for idx in range(len(df)):
        rec: Dict[str, Any] = {f: "" for f in CANONICAL_FIELDS}
        for col, canon in col_to_canon.items():
            val = df.iloc[idx][col]
            if canon == "collection_date":
                continue  # handled below from the raw value
            rec[canon] = "" if pd.isna(val) else str(val).strip()
        rec["row"] = idx + 2  # 1-based + header row

        # organism falls back to the preset's fixed organism
        if not rec["organism"]:
            rec["organism"] = preset.get("organism", "") or ""

        # ISO date
        raw_date = None
        for col, canon in col_to_canon.items():
            if canon == "collection_date":
                raw_date = df.iloc[idx][col]
                break
        iso, derr = normalize_collection_date(raw_date)
        rec["collection_date"] = iso
        rec["_date_error"] = derr

        # INSDC geo_loc_name
        geo, gerr = normalize_geo_loc_name(rec["geo_loc_name"], rec["region"], default_country)
        rec["geo_loc_name"] = geo
        rec["_geo_error"] = gerr

        rec["excluded"] = False
        rec["exclude_reason"] = ""
        if rec["sample_id"]:
            samples.append(rec)
        else:
            warnings.append(f"row {rec['row']}: blank sample id — skipped")
    return samples, warnings


# ---------------------------------------------------------------------------
# Metadata-level dedup (before NCBI lookups)
# ---------------------------------------------------------------------------
def dedup(samples: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Mark duplicate rows and -original/-repeat pairs excluded, in place.

    Keeps the first occurrence of an exact duplicate sample_id, and for an
    original/repeat pair keeps the -repeat. Returns the same list."""
    seen_ids: set = set()
    for s in samples:
        sid = s["sample_id"]
        if sid in seen_ids:
            s["excluded"] = True
            s["exclude_reason"] = "duplicate sample_id within the metadata sheet"
        seen_ids.add(sid)

    # Group surviving rows by base isolate; if both original and repeat exist,
    # exclude the original(s).
    by_base: Dict[str, List[Dict[str, Any]]] = {}
    for s in samples:
        if s["excluded"]:
            continue
        by_base.setdefault(base_isolate(s["sample_id"]), []).append(s)
    for base, group in by_base.items():
        if len(group) < 2:
            continue
        has_repeat = any(is_repeat(g["sample_id"]) for g in group)
        if has_repeat:
            for g in group:
                if not is_repeat(g["sample_id"]):
                    g["excluded"] = True
                    g["exclude_reason"] = f"-original superseded by a -repeat for base {base}"
    return samples
