#!/usr/bin/env python
"""
ncbi_pipeline.py — orchestrator for the NCBI Submit GUI.

Two modes:
  prep    Validate metadata, QC the inputs, check NCBI for existing records,
          build the upload-ready files (SRA templates + organized FASTQs, and/or
          GenBank concatenated FASTA + source table), and the PDF/Excel report.
  submit  Everything in prep, then build submission.xml and (unless --dry-run)
          push it to the NCBI submission FTP and poll report.xml for accessions.

Every step soft-fails: a problem in one stage is logged and the run continues so
the report still captures what succeeded. Provenance (every option, tool
version, and standard applied) is written to run_manifest.json.

Usage:
  ncbi_pipeline.py --mode {prep,submit} --archive {sra,genbank,both}
      --organism <preset> --metadata <xlsx> --outdir <run_dir>
      [--fastq-dir DIR] [--fasta-dir DIR] [--indir DIR]
      [--target {test,prod}] [--dry-run] [--no-ncbi-check] [--poll-seconds N]
"""

from __future__ import annotations

# --- provenance: log every external command this pipeline runs (best-effort) ---
# Attribute-level wrap of subprocess.Popen (which run/call/check_* all funnel
# through) + os.system, so EVERY external tool command (kraken2, amrfinder,
# blastn, spades, raxml, …) is recorded once to
# <outdir>/.provenance/<tool>_commands.txt — the exact commands that produced the
# results in this folder. Never alters behaviour; logging failures are swallowed
# and the original call always runs, so it can't break the pipeline.
def _install_provenance_capture():
    import os as _o, subprocess as _s, shlex as _sh
    from pathlib import Path as _P
    from datetime import datetime as _dt
    _tool = _P(__file__).resolve().parents[1].name
    _out = _P.cwd() / ".provenance"
    _f = _out / (_tool + "_commands.txt")
    def _log(_cmd):
        try:
            _out.mkdir(parents=True, exist_ok=True)
            _ln = _cmd if isinstance(_cmd, str) else _sh.join(str(c) for c in _cmd)
            _ts = _dt.now().astimezone().strftime("%H:%M:%S")
            with open(_f, "a", encoding="utf-8") as _h:
                _h.write(_ts + "  " + _ln + "\n")
        except Exception:
            pass
    try:
        _out.mkdir(parents=True, exist_ok=True)
        with open(_f, "a", encoding="utf-8") as _h:
            _h.write("\n# === %s run %s — external commands that produced results in this folder ===\n"
                     % (_tool, _dt.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")))
    except Exception:
        pass
    _orig_popen = _s.Popen
    class _Popen(_orig_popen):
        def __init__(self, args, *a, **k):
            _log(args)
            super().__init__(args, *a, **k)
    _s.Popen = _Popen
    _osys = _o.system
    def _sysw(_cmd):
        _log(_cmd)
        return _osys(_cmd)
    _o.system = _sysw
try:
    _install_provenance_capture()
except Exception:
    pass
# --- end provenance ------------------------------------------------------------


import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import presets
import metadata as meta
import validate as qc
import ncbi_eutils
import sra_prep
import genbank_prep
import submit_ftp


def log(msg: str) -> None:
    print(msg, flush=True)


def step(title: str) -> None:
    log("")
    log(f"### {title}")


def _tool_version(cmd: List[str]) -> str:
    import subprocess
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return (p.stdout or p.stderr or "").strip().splitlines()[0] if (p.stdout or p.stderr) else "?"
    except (OSError, subprocess.SubprocessError, IndexError):
        return "?"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="NCBI Submit pipeline orchestrator.")
    ap.add_argument("--mode", choices=["prep", "submit"], default="prep")
    ap.add_argument("--archive", choices=["sra", "genbank", "both"], default="both")
    ap.add_argument("--organism", default="generic", help="organism preset name")
    ap.add_argument("--metadata", type=Path, required=True)
    ap.add_argument("--outdir", type=Path, required=True)
    ap.add_argument("--indir", type=Path, default=None, help="project dir (fallback for fastq/fasta dirs)")
    ap.add_argument("--fastq-dir", type=Path, default=None)
    ap.add_argument("--fasta-dir", type=Path, default=None)
    ap.add_argument("--target", choices=["test", "prod"], default="test")
    ap.add_argument("--dry-run", action="store_true", default=False)
    ap.add_argument("--no-ncbi-check", action="store_true", default=False)
    ap.add_argument("--poll-seconds", type=int, default=0)
    args = ap.parse_args(argv)

    outdir: Path = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    date_stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")

    fastq_dir = args.fastq_dir or (args.indir / "download" if args.indir else None)
    fasta_dir = args.fasta_dir or (args.indir / "assemblies" if args.indir else None)
    if fastq_dir and not fastq_dir.is_dir() and args.indir:
        fastq_dir = args.indir
    if fasta_dir and not fasta_dir.is_dir() and args.indir:
        fasta_dir = args.indir

    log("=" * 70)
    log(f"NCBI Submit pipeline — mode={args.mode} archive={args.archive} organism={args.organism}")
    log(f"  metadata: {args.metadata}")
    log(f"  outdir:   {outdir}")
    log(f"  fastq:    {fastq_dir}    fasta: {fasta_dir}")
    log(f"  target:   {args.target}  dry_run={args.dry_run}")
    log("=" * 70)

    # ---- Load config-driven preset + standards ----
    preset = presets.load_preset(args.organism)
    packages = presets.load_packages()
    standards = presets.load_standards()
    package = packages.get(preset.get("biosample_package", "Generic.1.0"), {})
    thresholds = standards.get("qc_thresholds", {})

    # ---- Read + normalize metadata ----
    step("Reading metadata workbook")
    samples, warns = meta.read_metadata(args.metadata, preset)
    for w in warns:
        log(f"  - {w}")
    log(f"  {len(samples)} sample row(s)")
    meta.dedup(samples)
    n_excluded = sum(1 for s in samples if s.get("excluded"))
    log(f"  {n_excluded} excluded by metadata dedup (duplicates / -original superseded)")

    # ---- NCBI existence + BioSample crosswalk ----
    crosswalk: List[Dict[str, str]] = []
    if not args.no_ncbi_check:
        step("Checking NCBI for existing records + BioSample links")
        for s in samples:
            if s.get("excluded"):
                continue
            sid = s["sample_id"]
            link = ncbi_eutils.biosample_link(sid)
            if link.get("biosample") and not s.get("biosample"):
                s["biosample"] = link["biosample"]
                if link.get("bioproject") and not s.get("bioproject"):
                    s["bioproject"] = link["bioproject"]
            sra_acc = ncbi_eutils.sra_exists(sid) if args.archive in ("sra", "both") else None
            gb_acc = ncbi_eutils.genbank_exists(sid) if args.archive in ("genbank", "both") else None
            s["sra_existing"] = sra_acc or ""
            s["gb_existing"] = gb_acc or ""
            note = []
            if s.get("biosample"):
                note.append(f"BioSample {s['biosample']} (reuse)")
            if sra_acc:
                note.append(f"already in SRA: {sra_acc}")
            if gb_acc:
                note.append(f"already in GenBank: {gb_acc}")
            crosswalk.append({
                "sample": sid,
                "base": meta.base_isolate(sid),
                "biosample": s.get("biosample", ""),
                "bioproject": s.get("bioproject", ""),
                "sra_existing": sra_acc or "",
                "genbank_existing": gb_acc or "",
                "note": "; ".join(note),
            })
            if note:
                log(f"  {sid}: {'; '.join(note)}")
    else:
        log("NCBI existence check skipped (--no-ncbi-check).")

    # Write the crosswalk + duplicate-mask file.
    _write_crosswalk(outdir, crosswalk, date_stamp)

    # ---- Validate (QC + standards) ----
    step("Validating inputs (QC + ISO/INSDC standards)")
    verdicts: List[Dict[str, Any]] = []
    for s in samples:
        if s.get("excluded"):
            continue
        fqs = sra_prep.find_fastqs(fastq_dir, s["sample_id"]) if fastq_dir else []
        fa = genbank_prep.find_fasta(fasta_dir, s["sample_id"]) if fasta_dir else None
        v = qc.validate_sample(s, fqs, fa, package, thresholds)
        verdicts.append(v)
        if v["verdict"] != "pass":
            log(f"  {s['sample_id']}: {v['verdict'].upper()} — {'; '.join(v['notes']) or 'see report'}")
    n_pass = sum(1 for v in verdicts if v["verdict"] == "pass")
    n_review = sum(1 for v in verdicts if v["verdict"] == "review")
    n_fail = sum(1 for v in verdicts if v["verdict"] == "fail")
    log(f"  QC: {n_pass} pass · {n_review} review · {n_fail} fail")
    (outdir / "qc.json").write_text(json.dumps(verdicts, indent=2) + "\n", encoding="utf-8")

    # ---- Build upload-ready files ----
    sra_result: Dict[str, Any] = {}
    gb_result: Dict[str, Any] = {}
    if args.archive in ("sra", "both"):
        step("Building SRA submission files")
        sra_result = sra_prep.build_sra(samples, fastq_dir or Path("."), outdir, preset, date_stamp, log=log)
    if args.archive in ("genbank", "both"):
        step("Building GenBank submission files")
        gb_result = genbank_prep.build_genbank(samples, fasta_dir or Path("."), outdir, preset, date_stamp, log=log)

    # ---- Excluded-samples report ----
    _write_excluded_report(outdir, samples, verdicts, date_stamp)

    # ---- submission.xml (+ optional FTP submit) ----
    submission: Dict[str, Any] = {"built": False, "submitted": False, "report": {}}
    if args.mode == "submit":
        step("Building submission.xml")
        from app_config_bridge import load_contact_and_creds
        contact, creds = load_contact_and_creds()
        xml_path = submit_ftp.build_submission_xml(
            samples, preset, args.archive, sra_result, gb_result, outdir, contact, log=log
        )
        problems = submit_ftp.validate_submission_xml(xml_path)
        submission["built"] = True
        submission["xml"] = str(xml_path)
        submission["validation_problems"] = problems
        for p in problems:
            log(f"  ! {p}")
        if args.dry_run:
            log("  --dry-run: submission.xml built and validated; nothing uploaded.")
        elif problems:
            log("  Not submitting: submission.xml has validation problems (see above).")
        else:
            step(f"Submitting to NCBI ({args.target})")
            try:
                data_files = submit_ftp._collect_data_files(args.archive, sra_result, gb_result)
                sub_name = f"{args.organism}_{date_stamp}"
                remote = submit_ftp.ftp_submit(xml_path, data_files, creds, args.target, sub_name, log=log)
                submission["remote_dir"] = remote
                submission["submitted"] = True
                report = submit_ftp.poll_report(creds, remote, outdir, max_wait_s=args.poll_seconds, log=log)
                submission["report"] = report
                acc_path = submit_ftp.write_accessions_tsv(report, outdir)
                if acc_path:
                    log(f"  wrote {Path(acc_path).name}")
            except Exception as exc:  # noqa: BLE001 — never crash the run
                log(f"  ERROR during submission: {exc}")
                submission["error"] = str(exc)

    # ---- Provenance manifest ----
    manifest = {
        "tool": "ncbi_submit_gui",
        "mode": args.mode,
        "archive": args.archive,
        "organism_preset": args.organism,
        "biosample_package": preset.get("biosample_package"),
        "date": date_stamp,
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "options": {
            "target": args.target,
            "dry_run": args.dry_run,
            "ncbi_check": not args.no_ncbi_check,
        },
        "counts": {
            "rows": len(samples),
            "excluded": n_excluded,
            "qc_pass": n_pass, "qc_review": n_review, "qc_fail": n_fail,
        },
        "versions": {
            "table2asn": _tool_version(["table2asn", "-version"]),
            "seqkit": _tool_version(["seqkit", "version"]),
            "python": sys.version.split()[0],
        },
        "standards": standards.get("standards", []),
        "sra_result": sra_result,
        "genbank_result": gb_result,
        "submission": submission,
    }
    (outdir / "run_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # ---- Report (PDF + single-column stats xlsx) ----
    step("Building report (stats.xlsx + report.pdf)")
    try:
        import reporting
        reporting.build(outdir, args.organism, date_stamp, log=log)
    except Exception as exc:  # noqa: BLE001
        log(f"  WARNING: report generation failed: {exc}")

    step("Pipeline completed")
    log(f"Outputs in: {outdir}")
    return 0


def _write_crosswalk(outdir: Path, crosswalk: List[Dict[str, str]], date_stamp: str) -> None:
    cols = ["sample", "base", "biosample", "bioproject", "sra_existing", "genbank_existing", "note"]
    cw = outdir / f"ncbi_crosswalk_{date_stamp}.tsv"
    with cw.open("w", encoding="utf-8") as fh:
        fh.write("\t".join(cols) + "\n")
        for r in crosswalk:
            fh.write("\t".join(str(r.get(c, "")) for c in cols) + "\n")
    # duplicate_upload_to_mask.tsv: rows already present in an archive.
    masked = [r for r in crosswalk if r.get("sra_existing") or r.get("genbank_existing")]
    if masked:
        mp = outdir / "duplicate_upload_to_mask.tsv"
        with mp.open("w", encoding="utf-8") as fh:
            fh.write("sample\tSRA\tBioSample\tNotes\n")
            for r in masked:
                fh.write(f"{r['sample']}\t{r.get('sra_existing','')}\t{r.get('biosample','')}\t{r.get('note','')}\n")


def _write_excluded_report(outdir: Path, samples, verdicts, date_stamp: str) -> None:
    vmap = {v["sample_id"]: v for v in verdicts}
    lines = [f"Excluded / flagged samples — {date_stamp}", "=" * 60, ""]
    any_excluded = False
    for s in samples:
        if s.get("excluded"):
            any_excluded = True
            lines.append(f"[EXCLUDED] {s['sample_id']} (row {s.get('row')}): {s.get('exclude_reason')}")
    lines.append("")
    for s in samples:
        if s.get("excluded"):
            continue
        v = vmap.get(s["sample_id"])
        if v and v["verdict"] != "pass":
            any_excluded = True
            lines.append(f"[{v['verdict'].upper()}] {s['sample_id']}: {'; '.join(v['notes'])}")
    if not any_excluded:
        lines.append("All samples passed dedup and QC.")
    (outdir / f"excluded_samples_report_{date_stamp}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
