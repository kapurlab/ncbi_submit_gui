"""
NCBI Submit GUI — report builder.

From a finished run directory produces two deliverables:

  ncbi_submit_<date>_stats.xlsx
      A single labeled column of statistics (column A = label, column B =
      value), modelled on the vSNP3 stats workbook: input-file QC, dedup /
      existing-record counts, per-archive submission summary, and the standards
      applied — one flat, labeled list.

  report.pdf
      A human-readable PDF: input-file quality, a plain-language analysis
      summary, the submission results (prepared files + any harvested
      accessions), and a methods/standards + provenance page.

Both are best-effort: a missing artifact or optional dependency degrades
gracefully and is logged rather than failing the run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _fmt_int(v: Any) -> str:
    try:
        return f"{int(float(v)):,}"
    except (TypeError, ValueError):
        return "—" if v in (None, "") else str(v)


def _fastq_q_summary(verdicts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate read-quality across all samples for the stats sheet."""
    q30s, reads = [], []
    for v in verdicts:
        for st in (v.get("fastq_qc") or {}).values():
            if st.get("q30_pct") is not None:
                q30s.append(st["q30_pct"])
            if st.get("num_seqs") is not None:
                reads.append(st["num_seqs"])
    return {
        "mean_q30": round(sum(q30s) / len(q30s), 2) if q30s else None,
        "min_q30": round(min(q30s), 2) if q30s else None,
        "total_reads": int(sum(reads)) if reads else None,
        "n_fastq": len(q30s),
    }


def build_stats_items(
    manifest: Dict[str, Any],
    verdicts: List[Dict[str, Any]],
    date_stamp: str,
) -> List[Tuple[str, str]]:
    items: List[Tuple[str, str]] = []
    counts = manifest.get("counts", {}) or {}
    sra = manifest.get("sra_result", {}) or {}
    gb = manifest.get("genbank_result", {}) or {}
    sub = manifest.get("submission", {}) or {}
    opts = manifest.get("options", {}) or {}
    vers = manifest.get("versions", {}) or {}

    items.append(("Pipeline", manifest.get("tool", "ncbi_submit_gui")))
    items.append(("Date", date_stamp))
    items.append(("Mode", manifest.get("mode", "—")))
    items.append(("Archive", manifest.get("archive", "—")))
    items.append(("Organism preset", manifest.get("organism_preset", "—")))
    items.append(("BioSample package", manifest.get("biosample_package", "—")))
    items.append(("Submission target", opts.get("target", "—")))

    items.append(("Metadata rows", _fmt_int(counts.get("rows"))))
    items.append(("Excluded (dedup)", _fmt_int(counts.get("excluded"))))
    items.append(("QC pass", _fmt_int(counts.get("qc_pass"))))
    items.append(("QC review", _fmt_int(counts.get("qc_review"))))
    items.append(("QC fail", _fmt_int(counts.get("qc_fail"))))

    fq = _fastq_q_summary(verdicts)
    items.append(("FASTQ files QC'd", _fmt_int(fq.get("n_fastq"))))
    items.append(("Total reads (all FASTQ)", _fmt_int(fq.get("total_reads"))))
    items.append(("Mean Q30 (%)", str(fq.get("mean_q30")) if fq.get("mean_q30") is not None else "—"))
    items.append(("Min Q30 (%)", str(fq.get("min_q30")) if fq.get("min_q30") is not None else "—"))

    items.append(("SRA BioProjects", ", ".join(sra.get("bioprojects", [])) or "—"))
    items.append(("SRA runs prepared", _fmt_int(sra.get("n_runs"))))
    items.append(("SRA missing FASTQ", _fmt_int(len(sra.get("missing_fastq", []))) if sra else "—"))
    items.append(("GenBank sequences", _fmt_int(gb.get("n_sequences"))))
    items.append(("GenBank .sqn built", "yes" if gb.get("sqn") else "no"))

    items.append(("submission.xml built", "yes" if sub.get("built") else "no"))
    items.append(("Submitted to NCBI", "yes" if sub.get("submitted") else "no"))
    report = sub.get("report", {}) or {}
    items.append(("Submission status", report.get("status", "—")))
    items.append(("Accessions assigned", _fmt_int(len(report.get("accessions", []))) if report else "—"))

    items.append(("table2asn version", vers.get("table2asn", "—")))
    items.append(("seqkit version", vers.get("seqkit", "—")))
    std = [s.get("standard") for s in (manifest.get("standards") or []) if s.get("standard")]
    items.append(("Standards applied", "; ".join(std) if std else "—"))
    return items


def build(outdir: Path, label: str, date_stamp: str, log=print) -> Dict[str, Optional[str]]:
    """Build stats.xlsx + report.pdf for a finished run dir. Never raises."""
    outdir = Path(outdir)
    result: Dict[str, Optional[str]] = {"stats_xlsx": None, "report_pdf": None}

    manifest = _load_json(outdir / "run_manifest.json") or {}
    verdicts = _load_json(outdir / "qc.json") or []
    if not isinstance(verdicts, list):
        verdicts = []

    items = build_stats_items(manifest, verdicts, date_stamp)

    try:
        from .stats_excel import write_stats_xlsx
        xlsx_path = outdir / f"ncbi_submit_{date_stamp}_stats.xlsx"
        write_stats_xlsx(items, xlsx_path, label)
        result["stats_xlsx"] = str(xlsx_path)
        log(f"  wrote {xlsx_path.name}")
    except Exception as exc:  # noqa: BLE001
        log(f"  WARNING: stats workbook not written: {exc}")

    try:
        from .pdf_report import write_pdf
        pdf_path = outdir / "report.pdf"
        ctx = {
            "label": label,
            "date": date_stamp,
            "manifest": manifest,
            "verdicts": verdicts,
            "stats_items": items,
        }
        write_pdf(ctx, pdf_path, outdir)
        result["report_pdf"] = str(pdf_path)
        log(f"  wrote {pdf_path.name}")
    except Exception as exc:  # noqa: BLE001
        log(f"  WARNING: PDF report not written ({exc}). Is reportlab installed?")

    return result
