# NCBI Submit GUI — Claude Code Context

> Read this before touching any code. This tool is part of the Kapur Lab OOD
> pipeline family. The full conventions + gotchas live in
> `docs/BUILDING_A_SIBLING_TOOL.md` (cloned from amr_plus_gui). Read that too.

## What this is

A web tool to **prepare and submit sequence data to NCBI**: SRA (FASTQ) and
GenBank (FASTA), with required metadata supplied as an **Excel workbook**. It
validates to ISO/INSDC standards, deduplicates (including against records
already in NCBI), builds upload-ready files, can submit programmatically over
the NCBI submission FTP, and emits a **PDF report** + a **single-labeled-column
Excel stats** file (like vSNP3).

Built as a **FastAPI backend + React (Vite) SPA**, deployed as an **Open
OnDemand batch_connect** app. Sibling of `vsnp_gui`, `kraken_id_parse_gui`,
`amr_plus_gui`, `mlst_gui`, `genoflu_gui`, `irma_gui` — same look, deploy model,
and shared project layout under `/srv/kapurlab/projects/`.

## Hard constraints (break these → silent breakage)

1. **All frontend URLs relative** (`fetch("./api/...")`, `new EventSource("./api/jobs/<id>/log")`).
   `vite.config.js` keeps `base: "./"`. The browser origin is the OOD server.
2. **FastAPI serves the SPA** from `frontend/dist/`. Rebuild after any
   `frontend/src` edit: `cd frontend && npm run build`.
3. **Use the tool's conda env Python** (`env/bin/python`); run `seqkit` /
   `table2asn` with `env/bin` on PATH.
4. **No secrets in the repo.** NCBI API key + FTP password come only from
   `~/.config/ncbi_submit_gui/config.json` or env vars. `/api/config` returns
   booleans for credential presence, never the values.
5. `before.sh` runs in the OOD parent (only place `find_port` works);
   `script.sh.erb` starts uvicorn.

## Layout

```
backend/app/        main.py (routes) · jobs.py (JobManager, marker "ncbi_submit") · config.py
bin/                ncbi_pipeline.py (orchestrator) + metadata/validate/ncbi_eutils/
                    sra_prep/genbank_prep/submit_ftp/presets/app_config_bridge + reporting/
config/organisms/   YAML presets (generic, influenza_a) — the multi-pathogen engine
config/ncbi/        biosample_packages.yaml · config/standards.yaml
deploy/             install.sh (conda env + frontend) · register_ood_apps.sh
ood/apps/           ncbi_submit_gui (prod) · ncbi_submit_gui_dev (branch picker)
frontend/src/       App.jsx · App.css (shared theme — do not restyle)
```

## Project on-disk layout (shared, vSNP-compatible)

```
<project>/
  download/      FASTQ inputs (SRA)
  assemblies/    FASTA inputs (GenBank)
  metadata/      the NCBI metadata workbook(s) (.xlsx)
  ncbi_submit/<run_id>/   per-run outputs (prepared files, submission.xml, report.pdf, stats.xlsx, accessions)
```

## Pipeline (bin/ncbi_pipeline.py)

`--mode {prep,submit} --archive {sra,genbank,both} --organism <preset>
--metadata X.xlsx --outdir RUN --indir PROJECT [--target test|prod] [--dry-run]`

prep: read+normalize metadata → dedup → NCBI existence/BioSample crosswalk →
QC/standards validate → build SRA templates + GenBank FASTA/source table →
report. submit: also build `submission.xml`, validate, and (unless `--dry-run`)
FTP-submit + poll `report.xml`.

## Adding a pathogen

Drop a new `config/organisms/<name>.yaml` (copy `generic.yaml`): set the fixed
organism/SRA/GenBank fields, the BioSample package, the column aliases, and a
`gene_map` for segmented genomes. No code change.

## Reloads

- `bin/` scripts → next run. `backend/app/` → new OOD session (or dev `--reload`).
- `frontend/src` → `npm run build` + new session. `ood/**` → `sudo deploy/register_ood_apps.sh`.

## What I can't self-verify

Live FTP submission needs the lab's NCBI **programmatic-submission account**
(request via gb-admin@ncbi.nlm.nih.gov). The submit path defaults to the NCBI
**test** server and supports `--dry-run` (build + validate `submission.xml`, no
upload). End-to-end real submission requires those credentials in Settings.
