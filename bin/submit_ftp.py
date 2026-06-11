"""
Programmatic NCBI submission: build submission.xml, push to the submission FTP,
and poll report.xml for accessions.

Protocol (NCBI "programmatic submission"):
  1. Build a submission.xml of Action blocks — register a BioSample per isolate
     (unless it already exists, in which case we reference its SAMN), add the
     SRA runs (FASTQ) and/or the GenBank sequences, all linked to the
     BioProject and to each other so one BioSample serves both archives.
  2. Connect to the submission FTP, create a unique folder under
     submit/<Test|Production>/, upload the data files + submission.xml, then
     drop an empty `submit.ready` to signal NCBI to ingest.
  3. Poll the folder for report.xml; parse status + assigned accessions.

The default target is the NCBI **test** area. `--dry-run` builds and validates
submission.xml locally and uploads nothing — the safe path that needs no
credentials and is what the automated tests exercise.

Reference: NCBI submission schema + github.com/enviro-lab/ncbi-submit.
"""

from __future__ import annotations

import ftplib
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.dom import minidom

_TARGET_DIRS = {"test": "Test", "prod": "Production"}


# ---------------------------------------------------------------------------
# submission.xml
# ---------------------------------------------------------------------------
def _sub(parent, tag, text=None, **attrs):
    el = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    if text is not None:
        el.text = str(text)
    return el


def build_submission_xml(
    samples: List[Dict[str, Any]],
    preset: Dict[str, Any],
    archive: str,
    sra_result: Optional[Dict[str, Any]],
    gb_result: Optional[Dict[str, Any]],
    outdir: Path,
    contact: Dict[str, str],
    log=print,
) -> Path:
    """Assemble submission.xml from the canonical samples + prepared files.

    `archive` is 'sra', 'genbank', or 'both'. Samples already in NCBI (carrying
    a biosample accession from the crosswalk) reference that SAMN instead of
    re-registering it.
    """
    ns = contact.get("spuid_namespace") or "ncbi_submit_gui"
    package = preset.get("biosample_package", "Generic.1.0")
    sra_cfg = preset.get("sra", {}) or {}

    root = ET.Element("Submission")
    desc = _sub(root, "Description")
    _sub(desc, "Comment", f"Submission prepared by ncbi_submit_gui on "
                          f"{datetime.now(timezone.utc).date().isoformat()}")
    org = _sub(desc, "Organization", type="institute", role="owner")
    _sub(org, "Name", contact.get("organization", "Unknown organization"))
    contact_el = _sub(org, "Contact", email=contact.get("email", ""))
    name_el = _sub(contact_el, "Name")
    _sub(name_el, "First", contact.get("first_name", ""))
    _sub(name_el, "Last", contact.get("last_name", ""))

    active = [s for s in samples if not s.get("excluded")]

    # --- BioSample actions (skip when the isolate already has a SAMN) ---
    for s in active:
        if (s.get("biosample") or "").strip():
            continue
        action = _sub(root, "Action")
        addfiles = _sub(action, "AddData", target_db="BioSample")
        data = _sub(addfiles, "Data", content_type="XML")
        xml_content = _sub(data, "XmlContent")
        bs = _sub(xml_content, "BioSample", schema_version="2.0")
        sid_el = _sub(bs, "SampleId")
        _sub(sid_el, "SPUID", s["sample_id"], spuid_namespace=ns)
        descr = _sub(bs, "Descriptor")
        _sub(descr, "Title", f"{s.get('organism','')} {s['sample_id']}".strip())
        org_el = _sub(bs, "Organism")
        _sub(org_el, "OrganismName", s.get("organism", ""))
        if (s.get("bioproject") or "").strip():
            bp = _sub(bs, "BioProject")
            _sub(bp, "PrimaryId", s["bioproject"], db="BioProject")
        _sub(bs, "Package", package)
        attrs = _sub(bs, "Attributes")
        for attr_name, field in (
            ("collection_date", "collection_date"),
            ("geo_loc_name", "geo_loc_name"),
            ("host", "host"),
            ("isolation_source", "isolation_source"),
            ("serotype", "serotype"),
        ):
            val = (s.get(field) or "").strip()
            if val:
                _sub(attrs, "Attribute", val, attribute_name=attr_name)
        ident = _sub(addfiles, "Identifier")
        _sub(ident, "SPUID", s["sample_id"], spuid_namespace=ns)

    # --- SRA actions ---
    if archive in ("sra", "both") and sra_result:
        fastq_root = Path(sra_result.get("fastq_root", ""))
        from sra_prep import find_fastqs, detect_instrument
        for s in active:
            bp = s.get("bioproject") or "NO_BIOPROJECT"
            fqs = find_fastqs(fastq_root / bp, s["sample_id"]) or find_fastqs(fastq_root, s["sample_id"])
            if not fqs:
                continue
            action = _sub(root, "Action")
            addfiles = _sub(action, "AddFiles", target_db="SRA")
            for fq in fqs:
                f_el = _sub(addfiles, "File", file_path=f"{bp}/{fq.name}")
                _sub(f_el, "DataType", "generic-data")
            instrument = detect_instrument(fqs[0], sra_cfg.get("instrument_model_default", ""))
            for name, val in (
                ("instrument_model", instrument),
                ("library_strategy", sra_cfg.get("library_strategy", "WGS")),
                ("library_source", sra_cfg.get("library_source", "GENOMIC")),
                ("library_selection", sra_cfg.get("library_selection", "RANDOM")),
                ("library_layout", "paired" if len(fqs) > 1 else "single"),
                ("platform", sra_cfg.get("platform", "ILLUMINA")),
                ("title", sra_cfg.get("title") or f"WGS of {s.get('organism','')}".strip()),
                ("design_description", sra_cfg.get("design_description", "")),
            ):
                if val:
                    _sub(addfiles, "Attribute", val, name=name)
            if (s.get("bioproject") or "").strip():
                ref = _sub(addfiles, "AttributeRefId", name="BioProject")
                refid = _sub(ref, "RefId")
                _sub(refid, "PrimaryId", s["bioproject"], db="BioProject")
            ref = _sub(addfiles, "AttributeRefId", name="BioSample")
            refid = _sub(ref, "RefId")
            if (s.get("biosample") or "").strip():
                _sub(refid, "PrimaryId", s["biosample"], db="BioSample")
            else:
                _sub(refid, "SPUID", s["sample_id"], spuid_namespace=ns)
            ident = _sub(addfiles, "Identifier")
            _sub(ident, "SPUID", f"{s['sample_id']}.sra", spuid_namespace=ns)

    # --- GenBank action (one AddFiles for the concatenated submission) ---
    if archive in ("genbank", "both") and gb_result:
        seq_file = gb_result.get("sqn") or gb_result.get("concatenated_fasta")
        if seq_file:
            action = _sub(root, "Action")
            addfiles = _sub(action, "AddFiles", target_db="GenBank")
            f_el = _sub(addfiles, "File", file_path=Path(seq_file).name)
            _sub(f_el, "DataType", "sequence-submission" if str(seq_file).endswith(".sqn") else "generic-data")
            src = gb_result.get("source_table")
            if src:
                sf = _sub(addfiles, "File", file_path=Path(src).name)
                _sub(sf, "DataType", "generic-data")
            _sub(addfiles, "Attribute", "BankIt", name="wizard")
            ident = _sub(addfiles, "Identifier")
            _sub(ident, "SPUID", "genbank_submission", spuid_namespace=ns)

    xml_path = outdir / "submission.xml"
    pretty = minidom.parseString(ET.tostring(root, encoding="utf-8")).toprettyxml(indent="  ")
    xml_path.write_text(pretty, encoding="utf-8")
    log(f"  wrote {xml_path.name} ({len(active)} sample(s), archive={archive})")
    return xml_path


def validate_submission_xml(xml_path: Path) -> List[str]:
    """Lightweight structural validation (no network). Returns a list of
    problems; empty == looks well-formed and has at least one Action."""
    problems: List[str] = []
    try:
        root = ET.fromstring(xml_path.read_text(encoding="utf-8"))
    except ET.ParseError as e:
        return [f"submission.xml is not well-formed XML: {e}"]
    if root.tag != "Submission":
        problems.append("root element is not <Submission>")
    if root.find("Description") is None:
        problems.append("missing <Description> block")
    actions = root.findall("Action")
    if not actions:
        problems.append("no <Action> blocks (nothing to submit)")
    return problems


# ---------------------------------------------------------------------------
# FTP transfer + report polling
# ---------------------------------------------------------------------------
def _collect_data_files(archive: str, sra_result, gb_result) -> List[Path]:
    files: List[Path] = []
    if archive in ("sra", "both") and sra_result:
        root = Path(sra_result.get("fastq_root", ""))
        if root.is_dir():
            files += [p for p in root.rglob("*.fastq.gz")]
    if archive in ("genbank", "both") and gb_result:
        for key in ("sqn", "concatenated_fasta", "source_table"):
            v = gb_result.get(key)
            if v:
                files.append(Path(v))
    return [f for f in files if f.exists()]


def ftp_submit(
    xml_path: Path,
    data_files: List[Path],
    creds: Dict[str, str],
    target: str,
    submission_name: str,
    log=print,
) -> str:
    """Upload submission.xml + data files into submit/<Test|Production>/<name>/
    and write submit.ready. Returns the remote submission directory path."""
    host = creds.get("ftp_host") or "ftp-private.ncbi.nlm.nih.gov"
    user = creds.get("ftp_user", "")
    pw = creds.get("ftp_pass", "")
    if not user or not pw:
        raise RuntimeError(
            "NCBI FTP credentials are not configured. Set ncbi_ftp_user / "
            "ncbi_ftp_pass in the GUI Settings (or NCBI_FTP_USER / NCBI_FTP_PASS)."
        )
    sub_root = _TARGET_DIRS.get(target, "Test")
    remote_dir = f"submit/{sub_root}/{submission_name}"
    log(f"  FTP {host} -> {remote_dir}")
    ftp = ftplib.FTP(host, timeout=120)
    try:
        ftp.login(user, pw)
        for part in remote_dir.split("/"):
            try:
                ftp.mkd(part)
            except ftplib.error_perm:
                pass  # already exists
            ftp.cwd(part)
        for f in data_files:
            log(f"    STOR {f.name} ({f.stat().st_size} bytes)")
            with f.open("rb") as fh:
                ftp.storbinary(f"STOR {f.name}", fh)
        with xml_path.open("rb") as fh:
            ftp.storbinary("STOR submission.xml", fh)
        # Signal NCBI the upload is complete.
        import io
        ftp.storbinary("STOR submit.ready", io.BytesIO(b""))
        log("    wrote submit.ready")
    finally:
        try:
            ftp.quit()
        except Exception:
            pass
    return remote_dir


def poll_report(
    creds: Dict[str, str],
    remote_dir: str,
    outdir: Path,
    max_wait_s: int = 0,
    interval_s: int = 60,
    log=print,
) -> Dict[str, Any]:
    """Fetch report.xml from the remote submission dir and parse status +
    accessions. With max_wait_s=0, fetch once and return. Returns a dict."""
    host = creds.get("ftp_host") or "ftp-private.ncbi.nlm.nih.gov"
    user, pw = creds.get("ftp_user", ""), creds.get("ftp_pass", "")
    deadline = time.monotonic() + max_wait_s
    last: Dict[str, Any] = {"status": "unknown", "accessions": [], "fetched": False}
    while True:
        report_local = outdir / "report.xml"
        try:
            ftp = ftplib.FTP(host, timeout=120)
            ftp.login(user, pw)
            ftp.cwd(remote_dir)
            with report_local.open("wb") as fh:
                ftp.retrbinary("RETR report.xml", fh.write)
            ftp.quit()
            last = _parse_report(report_local)
            last["fetched"] = True
            log(f"  report status: {last['status']}; {len(last['accessions'])} accession(s)")
        except (ftplib.all_errors, OSError) as e:
            log(f"  report.xml not available yet ({e})")
        if last.get("status") in ("processed-ok", "processed-error", "failed") or time.monotonic() >= deadline:
            break
        time.sleep(interval_s)
    return last


def _parse_report(path: Path) -> Dict[str, Any]:
    out: Dict[str, Any] = {"status": "unknown", "accessions": []}
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
    except (OSError, ET.ParseError):
        return out
    status_el = root.find(".//SubmissionStatus")
    if status_el is not None:
        out["status"] = status_el.get("status", "unknown")
    for obj in root.findall(".//Object"):
        acc = obj.get("accession")
        if acc:
            out["accessions"].append({
                "accession": acc,
                "spuid": obj.get("spuid", ""),
                "type": obj.get("target_db", ""),
            })
    return out


def write_accessions_tsv(report: Dict[str, Any], outdir: Path) -> Optional[Path]:
    accs = report.get("accessions") or []
    if not accs:
        return None
    path = outdir / "accessions.tsv"
    with path.open("w", encoding="utf-8") as fh:
        fh.write("spuid\ttype\taccession\n")
        for a in accs:
            fh.write(f"{a.get('spuid','')}\t{a.get('type','')}\t{a.get('accession','')}\n")
    return path
