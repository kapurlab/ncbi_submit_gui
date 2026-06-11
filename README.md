# NCBI Submit GUI

A web tool for preparing and submitting whole-genome sequencing data to **NCBI**
— **SRA** (FASTQ) and **GenBank** (FASTA) — from a single **Excel metadata
sheet**. Part of the Kapur Lab Open OnDemand pipeline family (sibling of
`vsnp_gui`, `kraken_id_parse_gui`, `amr_plus_gui`, `mlst_gui`, `genoflu_gui`,
`irma_gui`); shares their look, deploy model, and project layout.

## What it does

Give it a project containing FASTQs and/or FASTAs plus one Excel metadata
workbook, pick an organism preset, and it will:

1. **Normalize & validate** the metadata to ISO/INSDC standards — ISO 8601
   collection dates, INSDC `geo_loc_name` (`Country:Region`), and the required
   attributes for the chosen INSDC/MIxS **BioSample package**.
2. **Deduplicate** — duplicate rows, `-original` vs `-repeat` pairs, and records
   **already in NCBI** (via E-utilities). The **BioSample is linked across SRA
   and GenBank** so one archive reuses the other's `SAMN`.
3. **QC the inputs** — FASTQ Phred Q20/Q30, FASTA length/N50/ambiguous-base
   fraction (seqkit). Each sample gets a pass / review / fail verdict.
4. **Build upload-ready files** — per-BioProject SRA BioSample + run-metadata
   tables (TSV + xlsx) with reads colocated, and a GenBank concatenated FASTA
   (`<isolate>_<gene>` for segmented genomes) + source-modifier table; an
   optional `.sqn` via `table2asn`.
5. **Submit programmatically** (optional) — build `submission.xml`, push to the
   NCBI submission FTP (test by default, production opt-in), and poll
   `report.xml` for assigned accessions.
6. **Report** — a **PDF** (input QC, analysis summary, results + accessions,
   methods/standards) and a **single-labeled-column Excel** stats workbook like
   vSNP3.

It needs **no large local database** — NCBI lookups are online — so it installs
light and ports easily to other OOD systems.

## Quick start

```bash
deploy/install.sh --conda-base /srv/kapurlab/tools/miniforge3   # env + frontend
sudo deploy/register_ood_apps.sh                                # register OOD cards (root)
```

Then launch **NCBI Submit** from the OnDemand dashboard (Interactive Apps →
Bioinformatics). In **Settings**, enter your NCBI contact email, (optional) API
key, and — for real submission — your NCBI programmatic-submission FTP
credentials. The submission target defaults to NCBI's **test** area.

## Organism presets

`config/organisms/*.yaml` drive all organism-specific behavior. Ships
`generic` (organism from the metadata sheet) and `influenza_a` (8-segment gene
map, serotype parsing). Add a pathogen by copying `generic.yaml`.

## CLI (no GUI)

```bash
PYTHONPATH=bin env/bin/python bin/ncbi_pipeline.py \
  --mode prep --archive both --organism influenza_a \
  --metadata /path/to/metadata.xlsx --indir /srv/kapurlab/projects/<project> \
  --outdir /srv/kapurlab/projects/<project>/ncbi_submit/run1
```

`--mode submit --dry-run` additionally builds and validates `submission.xml`
without uploading.

## Credentials & security

NCBI API key and FTP password live only in `~/.config/ncbi_submit_gui/config.json`
(chmod 600) or environment variables (`NCBI_EMAIL`, `NCBI_API_KEY`,
`NCBI_FTP_HOST/USER/PASS`). They are never committed and never returned to the
browser.

See `CLAUDE.md` and `docs/BUILDING_A_SIBLING_TOOL.md` for development details.
