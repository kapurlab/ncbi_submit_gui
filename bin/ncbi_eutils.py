"""
NCBI E-utilities client for existence checks and the SRA<->GenBank BioSample
crosswalk.

This is the deduplication + linking brain ported from the lab's standalone
sra_metadata.py / genbank_metadata.py / sra_lookup.py — with the hardcoded API
key removed (it now comes from the env / per-user config only) and a single
rate-limited, retrying HTTP path.

What it answers, per sample:
  * Is this isolate already an SRA run?           -> sra_exists()
  * Is this isolate already in GenBank/nucleotide? -> genbank_exists()
  * What BioSample/BioProject/SRA links it?        -> biosample_link()

The BioSample link is the key to not double-registering: if an isolate is
already in SRA, its SAMN BioSample is reused for the GenBank deposit (and vice
versa) instead of creating a new one.
"""

from __future__ import annotations

import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_API_KEY = (os.environ.get("NCBI_API_KEY") or "").strip()
_EMAIL = (os.environ.get("NCBI_EMAIL") or "").strip()
# 3 req/s without a key, 10 with. Stay under either to avoid 429.
_MIN_INTERVAL = 0.11 if _API_KEY else 0.40
_RETRY_BACKOFFS = (1.0, 2.0, 4.0, 8.0, 16.0)
_RUN_PREFIXES = ("SRR", "ERR", "DRR")

_last_call_at = 0.0


def _get(endpoint: str, params: Dict[str, str], timeout: int = 30) -> bytes:
    """GET an eutils endpoint with rate limiting + exponential backoff on 429/5xx."""
    global _last_call_at
    q = dict(params)
    if _API_KEY:
        q.setdefault("api_key", _API_KEY)
    if _EMAIL:
        q.setdefault("email", _EMAIL)
    q.setdefault("tool", "ncbi_submit_gui")
    url = f"{_EUTILS}/{endpoint}?{urllib.parse.urlencode(q)}"
    for attempt in range(len(_RETRY_BACKOFFS) + 1):
        elapsed = time.monotonic() - _last_call_at
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                _last_call_at = time.monotonic()
                return resp.read()
        except urllib.error.HTTPError as e:
            _last_call_at = time.monotonic()
            if e.code in (429, 500, 502, 503, 504) and attempt < len(_RETRY_BACKOFFS):
                wait = _RETRY_BACKOFFS[attempt]
                logger.warning("eutils %s; backing off %.1fs (attempt %d)", e.code, wait, attempt + 1)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < len(_RETRY_BACKOFFS):
                time.sleep(_RETRY_BACKOFFS[attempt])
                continue
            raise
    raise RuntimeError("unreachable")


def _esearch_count(db: str, term: str) -> int:
    try:
        data = _get("esearch.fcgi", {"db": db, "term": term, "retmax": "1"})
        root = ET.fromstring(data)
        c = root.findtext(".//Count")
        return int(c) if c is not None else 0
    except (urllib.error.URLError, urllib.error.HTTPError, ET.ParseError, ValueError) as e:
        logger.warning("esearch %s '%s' failed: %s", db, term, e)
        return 0


def _esearch_ids(db: str, term: str, retmax: int = 20) -> List[str]:
    try:
        data = _get("esearch.fcgi", {"db": db, "term": term, "retmax": str(retmax)})
        root = ET.fromstring(data)
        return [e.text for e in root.findall(".//IdList/Id") if e.text]
    except (urllib.error.URLError, urllib.error.HTTPError, ET.ParseError) as e:
        logger.warning("esearch ids %s '%s' failed: %s", db, term, e)
        return []


def sra_exists(sample: str) -> Optional[str]:
    """Return an existing SRA run accession for `sample` (by Library Name /
    exact match), or None. Conservative: only quoted exact-field matches, to
    avoid false positives from broad text search (lesson from sra_lookup.py)."""
    for term in (f'"{sample}"[Library Name]', f'"{sample}"[All Fields]'):
        ids = _esearch_ids("sra", term, retmax=5)
        if not ids:
            continue
        try:
            data = _get("efetch.fcgi", {"db": "sra", "id": ",".join(ids), "rettype": "runinfo", "retmode": "text"})
        except (urllib.error.URLError, urllib.error.HTTPError):
            continue
        text = data.decode("utf-8", "replace")
        for line in text.splitlines()[1:]:
            acc = line.split(",")[0].strip().strip('"')
            if acc.startswith(_RUN_PREFIXES):
                return acc
    return None


def genbank_exists(sample: str) -> Optional[str]:
    """Return an existing GenBank/nucleotide accession for `sample` (matched as
    isolate or strain), or None."""
    for term in (f'{sample}[isolate]', f'{sample}[strain]', f'"{sample}"[All Fields]'):
        if _esearch_count("nucleotide", term) > 0:
            ids = _esearch_ids("nucleotide", term, retmax=1)
            return ids[0] if ids else "present"
    return None


def biosample_link(sample: str) -> Dict[str, Optional[str]]:
    """Resolve {biosample, bioproject, sra} for an isolate via the BioSample DB.

    This is what lets one archive reuse the other's BioSample: search BioSample
    by isolate, then read the cross-links NCBI records (the SRA run id and the
    BioProject) out of the docsum XML.
    """
    out: Dict[str, Optional[str]] = {"biosample": None, "bioproject": None, "sra": None}
    ids = _esearch_ids("biosample", f"({sample}[isolate])", retmax=1) or _esearch_ids("biosample", f"({sample})", retmax=1)
    if not ids:
        return out
    try:
        data = _get("esummary.fcgi", {"db": "biosample", "id": ids[0], "retmode": "xml"})
        root = ET.fromstring(data)
    except (urllib.error.URLError, urllib.error.HTTPError, ET.ParseError):
        return out
    # esummary returns the BioSample record as escaped XML inside <SampleData>.
    acc = None
    for docsum in root.findall(".//DocumentSummary"):
        acc = docsum.findtext("Accession") or acc
        sample_xml = docsum.findtext("SampleData")
        if sample_xml:
            try:
                inner = ET.fromstring(sample_xml)
                acc = inner.get("accession") or acc
                for ident in inner.findall(".//Ids/Id"):
                    if ident.get("db") == "SRA" and ident.text:
                        out["sra"] = ident.text
                for link in inner.findall(".//Links/Link"):
                    if link.get("target") == "bioproject" and link.get("label"):
                        out["bioproject"] = link.get("label")
                    elif link.get("target") == "bioproject" and link.text:
                        out["bioproject"] = link.text
            except ET.ParseError:
                pass
    out["biosample"] = acc
    return out
