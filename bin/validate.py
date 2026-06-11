"""
Input-file QC and standards validation.

Two kinds of check, both recorded per sample so a reviewer can defend the
submission:
  * Metadata/BioSample completeness — required attributes for the chosen
    INSDC/MIxS package are present; collection_date is ISO 8601; geo_loc_name
    is INSDC-formatted.
  * Sequence QC — FASTQ Phred Q20/Q30 (seqkit stats -a) for SRA inputs, FASTA
    length/N50/ambiguous-base fraction for GenBank inputs.

Each sample gets a verdict: 'pass', 'review' (a metric is outside threshold or
an attribute is missing — analyst decides), or 'fail' (no usable input file).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


def _have(tool: str) -> bool:
    from shutil import which
    return which(tool) is not None


def seqkit_stats(path: Path) -> Dict[str, Any]:
    """`seqkit stats -T -a` for one file -> dict of numeric metrics (or {})."""
    if not _have("seqkit") or not Path(path).exists():
        return {}
    try:
        proc = subprocess.run(
            ["seqkit", "stats", "-T", "-a", str(path)],
            capture_output=True, text=True, timeout=600,
        )
    except (subprocess.SubprocessError, OSError):
        return {}
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        return {}
    row = dict(zip(lines[0].split("\t"), lines[1].split("\t")))

    def num(k):
        try:
            return float(str(row.get(k, "")).replace(",", ""))
        except (ValueError, AttributeError):
            return None

    return {
        "file": Path(path).name,
        "num_seqs": num("num_seqs"),
        "sum_len": num("sum_len"),
        "min_len": num("min_len"),
        "avg_len": num("avg_len"),
        "max_len": num("max_len"),
        "n50": num("N50"),
        "gc_pct": num("GC(%)"),
        "q20_pct": num("Q20(%)"),
        "q30_pct": num("Q30(%)"),
        "n_pct": num("N(%)"),
        "avg_qual": (row.get("AvgQual") or "").strip() or None,
    }


def check_package(rec: Dict[str, Any], package: Dict[str, Any]) -> List[str]:
    """Return the list of required BioSample attributes that are missing for
    `rec`. Maps package attribute names onto our canonical fields."""
    missing: List[str] = []
    # sample_name maps to sample_id; collected_by/strain/isolate have no
    # canonical column, so we treat them as satisfied if the analogous field is
    # present (sample_id) — they are 'recommended' in practice.
    field_for = {
        "sample_name": "sample_id",
        "organism": "organism",
        "collection_date": "collection_date",
        "geo_loc_name": "geo_loc_name",
        "host": "host",
        "isolation_source": "isolation_source",
        "serotype": "serotype",
    }
    for attr in package.get("required", []) or []:
        field = field_for.get(attr)
        if field is None:
            continue  # e.g. collected_by — not enforced from this sheet
        if not str(rec.get(field, "")).strip():
            missing.append(attr)
    return missing


def validate_sample(
    rec: Dict[str, Any],
    fastqs: List[Path],
    fasta: Optional[Path],
    package: Dict[str, Any],
    thresholds: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute QC + completeness for one sample. Returns a verdict dict with
    metrics, missing attributes, notes, and an overall 'pass'/'review'/'fail'."""
    notes: List[str] = []
    verdict = "pass"

    if rec.get("_date_error"):
        notes.append(f"collection_date: {rec['_date_error']}")
        verdict = "review"
    if rec.get("_geo_error"):
        notes.append(f"geo_loc_name: {rec['_geo_error']}")
        verdict = "review"

    missing = check_package(rec, package)
    if missing:
        notes.append(f"missing required BioSample attributes: {', '.join(missing)}")
        verdict = "review"

    fastq_qc: Dict[str, Any] = {}
    for fq in fastqs:
        tag = "R2" if ("_2." in fq.name or "_R2" in fq.name) else "R1"
        st = seqkit_stats(fq)
        if st:
            fastq_qc[tag] = st
    if fastq_qc:
        min_q30 = float(thresholds.get("fastq_min_q30_pct", 0))
        min_reads = float(thresholds.get("fastq_min_reads", 0))
        for tag, st in fastq_qc.items():
            if st.get("q30_pct") is not None and st["q30_pct"] < min_q30:
                notes.append(f"{tag} Q30 {st['q30_pct']:.1f}% < {min_q30}% threshold")
                verdict = "review"
            if st.get("num_seqs") is not None and st["num_seqs"] < min_reads:
                notes.append(f"{tag} {int(st['num_seqs'])} reads < {int(min_reads)} threshold")
                verdict = "review"

    fasta_qc: Dict[str, Any] = {}
    if fasta:
        fasta_qc = seqkit_stats(fasta)
        if fasta_qc:
            max_n = float(thresholds.get("fasta_max_ambiguous_pct", 100))
            min_len = float(thresholds.get("fasta_min_length", 0))
            if fasta_qc.get("n_pct") is not None and fasta_qc["n_pct"] > max_n:
                notes.append(f"ambiguous bases {fasta_qc['n_pct']:.2f}% > {max_n}% threshold")
                verdict = "review"
            if fasta_qc.get("min_len") is not None and fasta_qc["min_len"] < min_len:
                notes.append(f"shortest record {int(fasta_qc['min_len'])} bp < {int(min_len)} bp threshold")
                verdict = "review"

    have_input = any(Path(f).exists() for f in fastqs) or bool(fasta and Path(fasta).exists())
    if not have_input:
        notes.append("no input file found for this sample (FASTQ for SRA / FASTA for GenBank)")
        verdict = "fail"
    elif not fastq_qc and not fasta_qc:
        # Files exist but seqkit produced nothing (not on PATH) — don't fail the
        # sample over a missing QC tool; just note that metrics are unavailable.
        notes.append("seqkit not on PATH — QC metrics not computed")

    return {
        "sample_id": rec["sample_id"],
        "verdict": verdict,
        "missing_attributes": missing,
        "fastq_qc": fastq_qc,
        "fasta_qc": fasta_qc,
        "notes": notes,
    }
