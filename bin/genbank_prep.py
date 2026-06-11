"""
Build GenBank submission files from FASTA inputs + canonical metadata.

Produces, under <outdir>/genbank/:
  * concatenated_<date>.fasta — all non-duplicate records, renamed
      <isolate>_<gene> for segmented organisms (preset gene_map order) or
      <isolate> for single-record organisms.
  * genbank_source_<date>.tsv — the 5-column-style source-modifier table NCBI
      ingests; first column Sequence_ID, then qualifiers. Columns match the
      lab's existing genbank_metadata_<date>.tsv:
        Sequence_ID isolate Organism mol-type isolation-source geo_loc_name
        collection-date host serotype BioSample BioProject
  * <isolate>.sqn (best-effort) — when table2asn and a template.sbt are present.

Serotype is parsed from the FASTA description via the preset's serotype_regex,
falling back to the metadata serotype column.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

SOURCE_COLUMNS = [
    "Sequence_ID", "isolate", "Organism", "mol-type", "isolation-source",
    "geo_loc_name", "collection-date", "host", "serotype", "BioSample", "BioProject",
]


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


def find_fasta(indir: Path, sample_id: str) -> Optional[Path]:
    """Find a sample's FASTA in `indir` by prefix match (.fasta/.fa/.fna)."""
    if not indir.is_dir():
        return None
    for ext in (".fasta", ".fa", ".fna"):
        hits = sorted(indir.glob(f"{sample_id}*{ext}"))
        if hits:
            return hits[0]
    return None


def _read_fasta(path: Path) -> List[Dict[str, str]]:
    """Minimal FASTA reader -> [{id, desc, seq}], no external dependency."""
    records: List[Dict[str, str]] = []
    cur: Optional[Dict[str, str]] = None
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith(">"):
                if cur:
                    records.append(cur)
                header = line[1:].strip()
                rid = header.split()[0] if header else ""
                cur = {"id": rid, "desc": header, "seq": ""}
            elif cur is not None:
                cur["seq"] += line.strip()
        if cur:
            records.append(cur)
    return records


def _extract_serotype(desc: str, regex: str) -> str:
    if not regex:
        return ""
    m = re.search(regex, desc)
    if not m:
        return ""
    return m.group(0).strip("()")


def _wrap(seq: str, width: int = 70) -> str:
    return "\n".join(seq[i:i + width] for i in range(0, len(seq), width))


def build_genbank(
    samples: List[Dict[str, Any]],
    indir: Path,
    outdir: Path,
    preset: Dict[str, Any],
    date_stamp: str,
    log=print,
) -> Dict[str, Any]:
    gb_root = outdir / "genbank"
    gb_root.mkdir(parents=True, exist_ok=True)
    gene_map: List[str] = preset.get("gene_map", []) or []
    segmented = bool(preset.get("segmented"))
    organism_default = preset.get("organism", "") or ""
    mol_type = (preset.get("genbank") or {}).get("mol_type", "genomic DNA")
    serotype_regex = preset.get("serotype_regex", "") or ""

    concat_path = gb_root / f"concatenated_{date_stamp}.fasta"
    source_path = gb_root / f"genbank_source_{date_stamp}.tsv"
    source_rows: List[Dict[str, str]] = []
    missing_files: List[str] = []
    seq_count = 0
    fasta_chunks: List[str] = []

    for s in samples:
        if s.get("excluded") or s.get("gb_existing"):
            continue
        sid = s["sample_id"]
        fasta = find_fasta(indir, sid)
        if not fasta:
            missing_files.append(sid)
            log(f"  WARNING: no FASTA found for {sid} in {indir}")
            continue
        records = _read_fasta(fasta)
        if not records:
            missing_files.append(sid)
            continue

        # serotype: from any record description, else metadata.
        serotype = s.get("serotype", "")
        if not serotype and serotype_regex:
            for r in records:
                serotype = _extract_serotype(r["desc"], serotype_regex)
                if serotype:
                    break

        for i, rec in enumerate(records):
            if segmented and gene_map:
                gene = gene_map[i] if i < len(gene_map) else f"seg{i+1}"
                seq_id = f"{sid}_{gene}"
            elif len(records) > 1:
                seq_id = f"{sid}_{i+1}"
            else:
                seq_id = sid
            fasta_chunks.append(f">{seq_id}\n{_wrap(rec['seq'])}\n")
            seq_count += 1
            source_rows.append({
                "Sequence_ID": seq_id,
                "isolate": sid,
                "Organism": s.get("organism") or organism_default,
                "mol-type": mol_type,
                "isolation-source": s.get("isolation_source", ""),
                "geo_loc_name": s.get("geo_loc_name", ""),
                "collection-date": s.get("collection_date", ""),
                "host": s.get("host", ""),
                "serotype": serotype,
                "BioSample": s.get("biosample", ""),
                "BioProject": s.get("bioproject", ""),
            })

    concat_path.write_text("".join(fasta_chunks), encoding="utf-8")
    with source_path.open("w", encoding="utf-8") as fh:
        fh.write("\t".join(SOURCE_COLUMNS) + "\n")
        for r in source_rows:
            fh.write("\t".join(str(r.get(c, "")) for c in SOURCE_COLUMNS) + "\n")
    log(f"  wrote {concat_path.name} ({seq_count} sequence(s)) + {source_path.name}")

    sqn = _try_table2asn(gb_root, concat_path, source_path, log)

    return {
        "n_sequences": seq_count,
        "concatenated_fasta": str(concat_path),
        "source_table": str(source_path),
        "sqn": sqn,
        "missing_fasta": missing_files,
    }


def _try_table2asn(gb_root: Path, fasta: Path, src_tsv: Path, log) -> Optional[str]:
    """Run table2asn to produce a .sqn if the tool and a template.sbt exist.

    Best-effort: GenBank also accepts the FASTA + source table directly via the
    portal/programmatic path, so a missing .sqn is not fatal.
    """
    if not _have("table2asn"):
        log("  NOTE: table2asn not on PATH — skipping .sqn (FASTA + source table are sufficient).")
        return None
    template = _REPO_ROOT_TEMPLATE()
    if not template or not template.is_file():
        log("  NOTE: no template.sbt at config/templates/template.sbt — skipping .sqn.")
        return None
    # table2asn wants the source table named <fasta-stem>.src to auto-pair it.
    src_link = fasta.with_suffix(".src")
    try:
        shutil.copyfile(src_tsv, src_link)
    except OSError:
        pass
    cmd = ["table2asn", "-t", str(template), "-i", str(fasta), "-a", "s", "-V", "v"]
    log(f"  $ {' '.join(cmd)}")
    try:
        subprocess.run(cmd, cwd=str(gb_root), timeout=1800, check=False)
    except (subprocess.SubprocessError, OSError) as exc:
        log(f"  WARNING: table2asn failed: {exc}")
        return None
    sqn = fasta.with_suffix(".sqn")
    return str(sqn) if sqn.is_file() else None


def _REPO_ROOT_TEMPLATE() -> Optional[Path]:
    t = Path(__file__).resolve().parent.parent / "config" / "templates" / "template.sbt"
    return t
