"""
NCBI Submit GUI PDF report (reportlab + matplotlib).

Pure-Python PDF — no headless browser — so it renders reliably on any OOD host.
matplotlib figures are best-effort.

Layout: title + status banner, plain-language analysis summary, input-file
quality (per-sample QC verdicts + read quality), submission results (prepared
files and any harvested accessions), and a methods/standards + provenance page
with a genotype/metadata-accuracy disclaimer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

TEAL = colors.HexColor("#4C8C8A")
TERRA = colors.HexColor("#C88F7A")
INK = colors.HexColor("#1F2A2E")
MUTED = colors.HexColor("#6E7B82")
BORDER = colors.HexColor("#E3DED6")
DANGER = colors.HexColor("#C46A6A")
SUCCESS = colors.HexColor("#6BAA75")
WARN = colors.HexColor("#D8B26E")


def _styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle("H1", parent=ss["Title"], textColor=INK, fontSize=20, spaceAfter=2))
    ss.add(ParagraphStyle("Sub", parent=ss["Normal"], textColor=MUTED, fontSize=10, spaceAfter=10))
    ss.add(ParagraphStyle("H2", parent=ss["Heading2"], textColor=TEAL, fontSize=13, spaceBefore=12, spaceAfter=4))
    ss.add(ParagraphStyle("Body", parent=ss["Normal"], textColor=INK, fontSize=9.5, leading=13, alignment=TA_LEFT, spaceAfter=4))
    ss.add(ParagraphStyle("Small", parent=ss["Normal"], textColor=MUTED, fontSize=8, leading=10))
    ss.add(ParagraphStyle("Cell", parent=ss["Normal"], textColor=INK, fontSize=8.5, leading=11))
    return ss


def _kv_table(rows, ss, col0=2.4 * inch, col1=4.4 * inch):
    data = [[Paragraph(f"<b>{k}</b>", ss["Cell"]), Paragraph(str(v), ss["Cell"])] for k, v in rows]
    t = Table(data, colWidths=[col0, col1])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, BORDER),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor("#FBFAF8")]),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def _banner(text, fill, ss):
    t = Table([[Paragraph(f'<font color="white"><b>{text}</b></font>', ss["Body"])]], colWidths=[6.9 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), fill),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
    ]))
    return t


def _grid(data, ss, col_in, small=False):
    style = ss["Small"] if small else ss["Cell"]
    # Header cells are Paragraph flowables, which ignore the table's TEXTCOLOR,
    # so give the first row its own white, bold style for contrast on the teal.
    hdr_style = ParagraphStyle("GridHdr", parent=style, textColor=colors.white,
                               fontName="Helvetica-Bold")
    body = [[Paragraph(str(c), hdr_style if i == 0 else style) for c in row]
            for i, row in enumerate(data)]
    t = Table(body, colWidths=[c * inch for c in col_in], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), TEAL),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F5F2")]),
        ("GRID", (0, 0), (-1, -1), 0.3, BORDER), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def _qc_bar(counts: Dict[str, Any], outpath: Path) -> bool:
    data = {"pass": counts.get("qc_pass", 0), "review": counts.get("qc_review", 0), "fail": counts.get("qc_fail", 0)}
    if not sum(data.values()):
        return False
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        labels = list(data.keys())
        vals = [data[k] for k in labels]
        cols = ["#6BAA75", "#D8B26E", "#C46A6A"]
        fig, ax = plt.subplots(figsize=(4.2, 2.2))
        ax.bar(labels, vals, color=cols)
        ax.set_title("Sample QC verdicts", color="#1F2A2E", fontsize=11)
        for i, v in enumerate(vals):
            ax.text(i, v, str(v), ha="center", va="bottom", fontsize=9)
        ax.spines[["top", "right"]].set_visible(False)
        fig.tight_layout()
        fig.savefig(outpath, dpi=150)
        plt.close(fig)
        return True
    except Exception:
        return False


def _img_h(path: Path, width_in: float) -> float:
    try:
        from PIL import Image as PILImage
        with PILImage.open(path) as im:
            w, h = im.size
        return width_in * (h / w) * inch
    except Exception:
        return 2.0 * inch


def write_pdf(ctx: Dict[str, Any], path: Path, outdir: Path) -> None:
    ss = _styles()
    man = ctx["manifest"]
    verdicts: List[Dict[str, Any]] = ctx.get("verdicts") or []
    counts = man.get("counts", {}) or {}
    sra = man.get("sra_result", {}) or {}
    gb = man.get("genbank_result", {}) or {}
    sub = man.get("submission", {}) or {}
    report = sub.get("report", {}) or {}
    opts = man.get("options", {}) or {}

    assets = outdir / "_report_assets"
    assets.mkdir(exist_ok=True)

    story: List[Any] = []
    story.append(Paragraph("NCBI Submission Report", ss["H1"]))
    story.append(Paragraph(
        f"{man.get('archive','—').upper()} · {man.get('organism_preset','—')} · {ctx['date']} · "
        f"target {opts.get('target','—')}", ss["Sub"]))

    # Status banner
    if sub.get("submitted"):
        st = report.get("status", "submitted")
        fill = SUCCESS if st in ("processed-ok",) else WARN
        story.append(_banner(f"Submitted to NCBI ({opts.get('target')}). Status: {st}; "
                             f"{len(report.get('accessions', []))} accession(s).", fill, ss))
    elif sub.get("built"):
        story.append(_banner("submission.xml prepared (dry-run / not uploaded).", MUTED, ss))
    else:
        story.append(_banner("Prepared upload-ready files (no programmatic submission this run).", MUTED, ss))
    story.append(Spacer(1, 8))

    # Analysis summary
    story.append(Paragraph("Analysis summary", ss["H2"]))
    summary = (
        f"Read {counts.get('rows', 0)} metadata row(s); {counts.get('excluded', 0)} excluded by "
        f"deduplication (duplicate IDs or -original superseded by -repeat). "
        f"QC verdicts: <b>{counts.get('qc_pass',0)}</b> pass, <b>{counts.get('qc_review',0)}</b> review, "
        f"<b>{counts.get('qc_fail',0)}</b> fail. "
    )
    if man.get("archive") in ("sra", "both"):
        summary += (f"SRA: {sra.get('n_runs', 0)} run(s) across BioProject(s) "
                    f"{', '.join(sra.get('bioprojects', [])) or '—'}. ")
    if man.get("archive") in ("genbank", "both"):
        summary += f"GenBank: {gb.get('n_sequences', 0)} sequence(s) prepared. "
    summary += ("Records already present in NCBI were detected via E-utilities and excluded from "
                "re-upload; their BioSample is reused to link SRA and GenBank.")
    story.append(Paragraph(summary, ss["Body"]))

    fig = assets / "qc_verdicts.png"
    if _qc_bar(counts, fig):
        story.append(Image(str(fig), width=4.0 * inch, height=_img_h(fig, 4.0)))

    # Input file quality
    story.append(Paragraph("Input file quality", ss["H2"]))
    qc_rows = [r for r in verdicts if (r.get("fastq_qc") or r.get("fasta_qc"))]
    if qc_rows:
        story.append(Paragraph("Per-sample QC. Q20/Q30 are the percentage of bases at or above those "
                              "Phred scores; for assemblies, N% is the ambiguous-base fraction.", ss["Body"]))
        hdr = ["Sample", "Verdict", "Reads/Seqs", "Q30%", "GC%", "N%", "Notes"]
        data = [hdr]
        for r in qc_rows[:40]:
            fq = next(iter((r.get("fastq_qc") or {}).values()), {})
            fa = r.get("fasta_qc") or {}
            src = fq or fa
            data.append([
                r.get("sample_id", ""), r.get("verdict", ""),
                _num(src.get("num_seqs")), _num(src.get("q30_pct")),
                _num(src.get("gc_pct")), _num(fa.get("n_pct")),
                "; ".join(r.get("notes", []))[:60],
            ])
        story.append(_grid(data, ss, [1.3, 0.7, 0.9, 0.6, 0.6, 0.5, 2.3], small=True))
        if len(qc_rows) > 40:
            story.append(Paragraph(f"… {len(qc_rows) - 40} more in qc.json.", ss["Small"]))
    else:
        story.append(Paragraph("No QC metrics available (seqkit not on PATH or no input files matched).", ss["Body"]))

    # Submission results
    story.append(Paragraph("Submission results", ss["H2"]))
    accs = report.get("accessions") or []
    if accs:
        hdr = ["SPUID", "Type", "Accession"]
        data = [hdr] + [[a.get("spuid", ""), a.get("type", ""), a.get("accession", "")] for a in accs[:60]]
        story.append(_grid(data, ss, [2.6, 1.4, 2.8]))
    else:
        story.append(Paragraph(
            "No accessions harvested yet. Prepared files are listed below and in the run directory; "
            "for a live submission, accessions appear once NCBI processes report.xml.", ss["Body"]))
    prepared = []
    for f in (sra.get("files") or []):
        prepared.append(Path(f).name)
    if gb.get("concatenated_fasta"):
        prepared.append(Path(gb["concatenated_fasta"]).name)
    if gb.get("source_table"):
        prepared.append(Path(gb["source_table"]).name)
    if prepared:
        story.append(Paragraph("Prepared files: " + ", ".join(prepared[:20]) +
                              (f" (+{len(prepared)-20} more)" if len(prepared) > 20 else ""), ss["Small"]))

    # Methods & provenance
    story.append(Paragraph("Methods &amp; standards", ss["H2"]))
    std = man.get("standards") or []
    rows = [(s.get("standard", ""), s.get("metric", "")) for s in std]
    if rows:
        story.append(_kv_table(rows, ss))
    vers = man.get("versions", {}) or {}
    story.append(Spacer(1, 4))
    story.append(_kv_table([
        ("BioSample package", man.get("biosample_package", "—")),
        ("table2asn", vers.get("table2asn", "—")),
        ("seqkit", vers.get("seqkit", "—")),
        ("NCBI existence check", "on" if opts.get("ncbi_check") else "off"),
        ("Submission target", opts.get("target", "—")),
    ], ss))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "Disclaimer: this report documents the metadata, QC, and submission actions prepared by "
        "ncbi_submit_gui. Accession assignment, validation, and any release are governed by NCBI. "
        "Verify the harvested accessions against the NCBI Submission Portal before citing them. "
        "Metadata accuracy (collection date, geography, host) is the submitter's responsibility; this "
        "tool normalizes formats (ISO 8601 dates, INSDC geo_loc_name) but cannot confirm correctness.",
        ss["Small"]))

    doc = SimpleDocTemplate(
        str(path), pagesize=letter,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch, leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        title=f"NCBI Submission report — {ctx.get('label','')}", author="ncbi_submit_gui",
    )
    doc.build(story)


def _num(v):
    try:
        f = float(v)
        return f"{int(f):,}" if f.is_integer() else f"{f:.2f}"
    except (TypeError, ValueError):
        return "—" if v in (None, "") else str(v)
