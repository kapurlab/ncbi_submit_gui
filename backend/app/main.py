"""
NCBI Submit GUI — FastAPI backend.

Serves the React SPA from frontend/dist/ and provides:
  /api/projects                         — list shared + personal projects
  /api/projects/{n}/inputs              — list download/ (FASTQ) or assemblies/ (FASTA)
  /api/projects/{n}/upload              — upload FASTQ/FASTA/metadata
  /api/projects/{n}/link-local          — symlink local FASTQ/FASTA
  /api/projects/{n}/metadata            — list/upload the NCBI metadata workbook(s)
  /api/organism-presets                 — config-driven organism presets
  /api/config                           — get (public) / set user config + NCBI creds
  /api/run/prep                         — build upload-ready files + report
  /api/run/submit                       — build submission.xml + (optional) FTP submit
  /api/projects/{n}/submit-runs         — list past runs under ncbi_submit/
  /api/projects/{n}/runs/{run}/results  — files for a past run
  /api/jobs, /api/jobs/{id}, /api/jobs/{id}/log (SSE), /results, /file

Sibling of vsnp_gui / kraken_id_parse_gui / amr_plus_gui — shares the project
layout. All URLs are relative (uvicorn is behind the OOD rnode proxy).
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiofiles
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import SECRET_KEYS, load_config, public_config, save_config
from .jobs import JobManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
_BIN_DIR = _REPO_ROOT / "bin"
_CONFIG_DIR = _REPO_ROOT / "config"
_ORGANISMS_DIR = _CONFIG_DIR / "organisms"
_FRONTEND_DIST = _REPO_ROOT / "frontend" / "dist"
_SHARED_PROJECTS = Path("/srv/kapurlab/projects")
_JOBS_DIR = _REPO_ROOT / "backend" / "jobs"

app = FastAPI(title="NCBI Submit GUI")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
job_manager = JobManager(_JOBS_DIR)

_SCOPE_SHARED = "shared"
_SCOPE_PERSONAL = "personal"
_FASTQ_EXT = ".fastq.gz"
_FASTA_EXT = (".fasta", ".fa", ".fna")
_META_EXT = (".xlsx", ".xls")


def _safe_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime if p.is_dir() else 0
    except PermissionError:
        return 0


def _count_files(d: Path, exts) -> int:
    # Shared projects may belong to another user; stat/iterdir can raise
    # PermissionError. Treat "no access" as -1 (the frontend shows it as such)
    # rather than 500-ing the whole project list.
    try:
        if not d.is_dir():
            return 0
        return sum(1 for p in d.iterdir() if p.is_file() and p.name.lower().endswith(exts))
    except (PermissionError, OSError):
        return -1


def _list_projects_from_root(root: Path, scope: str) -> List[Dict]:
    if not root.is_dir():
        return []
    projects = []
    try:
        entries = sorted(root.iterdir(), key=_safe_mtime, reverse=True)
    except PermissionError:
        return []
    for p in entries:
        try:
            if not p.is_dir() or p.name.startswith("."):
                continue
        except PermissionError:
            continue
        submit_runs = []
        sd = p / "ncbi_submit"
        try:
            if sd.is_dir():
                submit_runs = [d.name for d in sorted(sd.iterdir(), key=_safe_mtime, reverse=True) if d.is_dir()]
        except PermissionError:
            pass
        projects.append({
            "name": p.name,
            "path": str(p),
            "scope": scope,
            "fastq_count": _count_files(p / "download", _FASTQ_EXT),
            "fasta_count": _count_files(p / "assemblies", _FASTA_EXT),
            "submit_runs": submit_runs,
        })
    return projects


def _get_project_dir(name: str) -> Optional[Path]:
    if "/" in name or name.startswith("."):
        return None
    cfg = load_config()
    for root in [_SHARED_PROJECTS, Path(cfg.get("projects_root", ""))]:
        candidate = root / name
        if candidate.is_dir():
            return candidate
    return None


_PROJECT_NAME_OK = re.compile(r"^[A-Za-z0-9._-]+$")


def _normalize_project_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("Project name must be a string")
    cleaned = re.sub(r"\s+", "_", name.strip())
    if not cleaned:
        raise ValueError("Project name is empty")
    if cleaned.startswith("."):
        raise ValueError("Project name cannot start with '.'")
    if len(cleaned) > 100:
        raise ValueError("Project name too long (max 100 characters)")
    if not _PROJECT_NAME_OK.match(cleaned):
        bad = sorted(set(ch for ch in cleaned if not re.match(r"[A-Za-z0-9._-]", ch)))
        raise ValueError(f"Project name contains unsupported characters: {''.join(bad)!r}. "
                         "Only letters, digits, _ - . are allowed (spaces become underscores).")
    return cleaned


def _ensure_project_dirs(project_dir: Path) -> None:
    (project_dir / "download").mkdir(parents=True, exist_ok=True)   # FASTQ (SRA)
    (project_dir / "assemblies").mkdir(parents=True, exist_ok=True)  # FASTA (GenBank)
    (project_dir / "metadata").mkdir(parents=True, exist_ok=True)    # NCBI metadata workbooks
    (project_dir / "ncbi_submit").mkdir(parents=True, exist_ok=True)  # per-run outputs
    # vSNP-compatible skeleton so the project is shared cleanly between tools.
    (project_dir / "step1").mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def _create_project(name: str, scope: str) -> Path:
    name = _normalize_project_name(name)
    cfg = load_config()
    root = _SHARED_PROJECTS if scope == _SCOPE_SHARED else Path(cfg.get("projects_root", "") or (Path.home() / "projects"))
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"Cannot create projects root {root}: {exc}")
    project_dir = root / name
    if project_dir.exists():
        raise ValueError(f"Project already exists: {name}")
    try:
        _ensure_project_dirs(project_dir)
    except PermissionError:
        raise ValueError(f"No permission to create a project under {root}. "
                         "Shared projects require lab write access; create it as a personal project instead.")
    try:
        with open(project_dir / "project.json", "w", encoding="utf-8") as f:
            json.dump({"name": name, "created_at": _now_iso(), "status": "created"}, f, indent=2, sort_keys=True)
    except OSError:
        pass
    return project_dir


def _kind_dir(project_dir: Path, kind: str) -> Path:
    return project_dir / ("assemblies" if kind == "fasta" else "download" if kind == "fastq" else "metadata")


# ---------------------------------------------------------------------------
# Project + input routes
# ---------------------------------------------------------------------------
@app.get("/api/projects")
def api_list_projects():
    cfg = load_config()
    projects = _list_projects_from_root(_SHARED_PROJECTS, _SCOPE_SHARED)
    personal_root = Path(cfg.get("projects_root", ""))
    if personal_root != _SHARED_PROJECTS:
        personal = _list_projects_from_root(personal_root, _SCOPE_PERSONAL)
        seen = {p["name"] for p in projects}
        projects += [p for p in personal if p["name"] not in seen]
    return JSONResponse(projects)


class ProjectCreate(BaseModel):
    name: str
    scope: Optional[str] = None


@app.post("/api/projects")
def api_create_project(payload: ProjectCreate):
    scope = (payload.scope or _SCOPE_PERSONAL).strip() or _SCOPE_PERSONAL
    if scope not in (_SCOPE_PERSONAL, _SCOPE_SHARED):
        raise HTTPException(400, f"Invalid scope: {scope!r}")
    try:
        project_dir = _create_project(payload.name, scope)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return JSONResponse({"name": project_dir.name, "path": str(project_dir), "scope": scope})


def _writable_project_dir(name: str) -> Path:
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    _ensure_project_dirs(project_dir)
    return project_dir


def _list_dir(d: Path) -> Dict[str, Any]:
    files: List[Dict] = []
    total = 0
    if d.is_dir():
        for p in sorted(d.iterdir()):
            if not p.is_file() or p.name.startswith("."):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            files.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
            total += st.st_size
    return {"files": files, "total_bytes": total, "count": len(files)}


@app.get("/api/projects/{name}/inputs")
def api_project_inputs(name: str, kind: str = Query("fastq")):
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    return JSONResponse(_list_dir(_kind_dir(project_dir, kind)))


@app.delete("/api/projects/{name}/inputs/{filename}")
def api_project_input_delete(name: str, filename: str, kind: str = Query("fastq")):
    if not filename or "/" in filename or "\\" in filename or filename.startswith(".") or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    target = _kind_dir(project_dir, kind) / filename
    if not target.is_file() and not target.is_symlink():
        raise HTTPException(404, f"File not found: {filename}")
    target.unlink()
    return JSONResponse({"deleted": filename})


def _accepts(kind: str):
    if kind == "fasta":
        return _FASTA_EXT
    if kind == "metadata":
        return _META_EXT
    return (_FASTQ_EXT,)


@app.post("/api/projects/{name}/upload")
async def api_project_upload(name: str, kind: str = Query("fastq"), files: List[UploadFile] = File(...)):
    project_dir = _writable_project_dir(name)
    dest = _kind_dir(project_dir, kind)
    accepts = _accepts(kind)
    saved = 0
    for f in files:
        if not f.filename or not f.filename.lower().endswith(accepts):
            continue
        target = dest / Path(f.filename).name
        async with aiofiles.open(target, "wb") as out:
            while True:
                chunk = await f.read(1024 * 1024)
                if not chunk:
                    break
                await out.write(chunk)
        saved += 1
    return JSONResponse({"uploaded": saved})


class LinkLocalRequest(BaseModel):
    path: str
    kind: Optional[str] = "fastq"


@app.post("/api/projects/{name}/link-local")
def api_project_link_local(name: str, payload: LinkLocalRequest):
    project_dir = _writable_project_dir(name)
    src = Path((payload.path or "").strip()).expanduser()
    if not src.exists():
        raise HTTPException(400, f"Input path not found: {src}")
    kind = payload.kind or "fastq"
    dest = _kind_dir(project_dir, kind)
    accepts = _accepts(kind)
    candidates = [src] if src.is_file() else sorted(
        f for f in src.iterdir() if f.is_file() and f.name.lower().endswith(accepts))
    count = 0
    for f in candidates:
        if not f.name.lower().endswith(accepts):
            continue
        target = dest / f.name
        if not target.exists():
            target.symlink_to(f.resolve())
            count += 1
    return JSONResponse({"linked": count})


@app.get("/api/projects/{name}/metadata")
def api_metadata_list(name: str):
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    return JSONResponse(_list_dir(project_dir / "metadata"))


# ---------------------------------------------------------------------------
# Organism presets + config
# ---------------------------------------------------------------------------
@app.get("/api/organism-presets")
def api_organism_presets():
    out = []
    if _ORGANISMS_DIR.is_dir():
        import yaml
        for f in sorted(_ORGANISMS_DIR.glob("*.yaml")):
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                data = {}
            nm = data.get("name") or f.stem
            out.append({"name": nm, "display_name": data.get("display_name", nm),
                        "package": data.get("biosample_package", "")})
    return JSONResponse(out)


@app.get("/api/config")
def api_get_config():
    return JSONResponse(public_config(load_config()))


class ConfigPayload(BaseModel):
    projects_root: Optional[str] = None
    saved_project_roots: Optional[List[str]] = None
    shared_projects_root: Optional[str] = None
    organism_preset: Optional[str] = None
    submit_target: Optional[str] = None
    ncbi_email: Optional[str] = None
    ncbi_api_key: Optional[str] = None
    ncbi_ftp_host: Optional[str] = None
    ncbi_ftp_user: Optional[str] = None
    ncbi_ftp_pass: Optional[str] = None
    ncbi_organization: Optional[str] = None
    ncbi_contact_first: Optional[str] = None
    ncbi_contact_last: Optional[str] = None


@app.post("/api/config")
def api_save_config(payload: ConfigPayload):
    cfg = load_config()
    updates = payload.model_dump(exclude_none=True)
    # Don't overwrite a stored secret with an empty string from the form.
    for k in SECRET_KEYS:
        if k in updates and not str(updates[k]).strip():
            updates.pop(k)
    cfg.update(updates)
    roots = cfg.get("saved_project_roots") or []
    if isinstance(roots, list):
        seen, cleaned = set(), []
        for r in roots:
            r = (r or "").strip()
            if r and r not in seen:
                seen.add(r); cleaned.append(r)
        cfg["saved_project_roots"] = cleaned
    save_config(cfg)
    return JSONResponse({"ok": True})


@app.get("/api/browse-dirs")
def api_browse_dirs(path: str = ""):
    try:
        p = (Path(path).expanduser() if path.strip() else Path.home()).resolve()
    except (OSError, RuntimeError):
        raise HTTPException(400, "Invalid path")
    if not p.is_dir():
        raise HTTPException(400, f"Not a directory: {p}")
    entries: List[Dict[str, str]] = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            if child.name.startswith("."):
                continue
            try:
                if child.is_dir():
                    entries.append({"name": child.name, "path": str(child)})
            except OSError:
                continue
    except PermissionError:
        raise HTTPException(403, f"Permission denied: {p}")
    parent = str(p.parent) if p.parent != p else None
    return JSONResponse({"path": str(p), "parent": parent, "entries": entries})


# ---------------------------------------------------------------------------
# Run: prep / submit
# ---------------------------------------------------------------------------
class RunPayload(BaseModel):
    project: str
    metadata: str                      # filename within <project>/metadata/
    organism: str = "generic"
    archive: str = "both"              # sra | genbank | both
    mode: str = "prep"                 # prep | submit
    target: Optional[str] = None       # test | prod (defaults to config)
    dry_run: bool = True
    no_ncbi_check: bool = False
    poll_seconds: int = 0


def _new_run_id(organism: str) -> str:
    from datetime import datetime
    return f"{organism}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


@app.post("/api/run/prep")
@app.post("/api/run/submit")
def api_run(payload: RunPayload, request: Request):
    project_dir = _get_project_dir(payload.project)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {payload.project}")
    mode = "submit" if request.url.path.endswith("/submit") else (payload.mode or "prep")

    meta_name = Path(payload.metadata).name
    meta_path = project_dir / "metadata" / meta_name
    if not meta_path.is_file():
        raise HTTPException(400, f"Metadata workbook not found: {meta_name} (upload it first)")
    if payload.archive not in ("sra", "genbank", "both"):
        raise HTTPException(400, f"Invalid archive: {payload.archive}")

    cfg = load_config()
    target = (payload.target or cfg.get("submit_target") or "test").strip()
    run_id = _new_run_id(payload.organism)
    run_dir = project_dir / "ncbi_submit" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    command = [sys.executable, "-u", str(_BIN_DIR / "ncbi_pipeline.py"),
               "--mode", mode, "--archive", payload.archive,
               "--organism", payload.organism, "--metadata", str(meta_path),
               "--outdir", str(run_dir), "--indir", str(project_dir),
               "--target", target]
    if mode == "submit" and payload.dry_run:
        command.append("--dry-run")
    if payload.no_ncbi_check:
        command.append("--no-ncbi-check")
    if payload.poll_seconds:
        command.extend(["--poll-seconds", str(int(payload.poll_seconds))])

    # Pass credentials to the pipeline subprocess via the environment so they
    # are never written into job state/logs. The pipeline reads them (env wins
    # over the config file) through app_config_bridge.
    env = {
        "PYTHONPATH": str(_BIN_DIR),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONUNBUFFERED": "1",
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
    }
    for key, env_name in (("ncbi_email", "NCBI_EMAIL"), ("ncbi_api_key", "NCBI_API_KEY"),
                          ("ncbi_ftp_host", "NCBI_FTP_HOST"), ("ncbi_ftp_user", "NCBI_FTP_USER"),
                          ("ncbi_ftp_pass", "NCBI_FTP_PASS"), ("ncbi_organization", "NCBI_ORGANIZATION"),
                          ("ncbi_contact_first", "NCBI_CONTACT_FIRST"), ("ncbi_contact_last", "NCBI_CONTACT_LAST")):
        v = str(cfg.get(key, "") or "").strip()
        if v:
            env[env_name] = v

    job_name = f"{payload.project}/{run_id} — NCBI {mode} ({payload.archive}, {target})"
    job_id = job_manager.start_job(name=job_name, command=command, cwd=run_dir, env=env)
    return JSONResponse({"job_id": job_id, "run_id": run_id, "run_dir": str(run_dir), "mode": mode})


# ---------------------------------------------------------------------------
# Past-run results
# ---------------------------------------------------------------------------
@app.get("/api/projects/{name}/submit-runs")
def api_submit_runs(name: str):
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    runs = []
    sd = project_dir / "ncbi_submit"
    if sd.is_dir():
        for d in sorted(sd.iterdir(), key=_safe_mtime, reverse=True):
            if not d.is_dir():
                continue
            man = d / "run_manifest.json"
            meta = {}
            if man.is_file():
                try:
                    meta = json.loads(man.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    meta = {}
            runs.append({"run_id": d.name, "archive": meta.get("archive"),
                         "mode": meta.get("mode"), "counts": meta.get("counts", {})})
    return JSONResponse(runs)


@app.get("/api/projects/{name}/runs/{run_id}/results")
def api_run_results(name: str, run_id: str, all: int = Query(0)):
    if "/" in run_id or run_id.startswith("."):
        raise HTTPException(400, "Invalid run id")
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    run_dir = project_dir / "ncbi_submit" / run_id
    return JSONResponse({"project": name, "run_id": run_id, "present": run_dir.is_dir(),
                         "run_dir": str(run_dir), "files": _collect_result_files(run_dir, bool(all))})


# ---------------------------------------------------------------------------
# Result categorization + file serving
# ---------------------------------------------------------------------------
_INLINE_MEDIA = {".pdf": "application/pdf", ".html": "text/html", ".htm": "text/html",
                 ".txt": "text/plain", ".log": "text/plain", ".json": "application/json",
                 ".tsv": "text/plain", ".xml": "application/xml", ".png": "image/png",
                 ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml", ".csv": "text/plain"}
_DOWNLOAD_MEDIA = {".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                   ".xls": "application/vnd.ms-excel", ".fasta": "text/plain", ".fa": "text/plain",
                   ".fna": "text/plain", ".sqn": "application/octet-stream", ".gz": "application/gzip"}


def _can_open_inline(name: str) -> bool:
    return Path(name).suffix.lower() in _INLINE_MEDIA


def _media_type_for(name: str) -> str:
    ext = Path(name).suffix.lower()
    return _INLINE_MEDIA.get(ext) or _DOWNLOAD_MEDIA.get(ext) or "application/octet-stream"


def _result_category(rel: str) -> Optional[str]:
    path = Path(rel)
    name = path.name
    if any(part.startswith(".") or part == "_report_assets" for part in path.parts):
        return None
    if name.endswith(".fastq.gz"):
        return None
    if name == "report.pdf":
        return "report_pdf"
    if name.endswith("_stats.xlsx"):
        return "stats_xlsx"
    if name == "submission.xml":
        return "submission_xml"
    if name == "report.xml":
        return "report_xml"
    if name == "accessions.tsv":
        return "accessions"
    if name == "duplicate_upload_to_mask.tsv":
        return "duplicate_mask"
    if name.startswith("ncbi_crosswalk") and name.endswith(".tsv"):
        return "crosswalk"
    if name.startswith("excluded_samples_report"):
        return "excluded"
    if name == "qc.json":
        return "qc"
    if name == "run_manifest.json":
        return "run_manifest"
    if name.endswith(".xlsx"):
        return "sra_xlsx"
    if name.startswith("concatenated_") and name.endswith(".fasta"):
        return "genbank_fasta"
    if name.startswith("genbank_source") and name.endswith(".tsv"):
        return "genbank_source"
    if name.endswith("_sra_metadata") or "_sra_metadata_" in name:
        return "sra_metadata"
    if "_biosample_attributes_" in name:
        return "biosample_attrs"
    if name.endswith(".tsv"):
        return "tsv"
    return None


_CATEGORY_ORDER = {
    "report_pdf": 0, "stats_xlsx": 1, "accessions": 2, "submission_xml": 3, "report_xml": 4,
    "sra_xlsx": 5, "sra_metadata": 6, "biosample_attrs": 7, "genbank_fasta": 8, "genbank_source": 9,
    "crosswalk": 10, "duplicate_mask": 11, "excluded": 12, "qc": 13, "run_manifest": 14, "tsv": 20, "log": 99,
}
_CATEGORY_LABEL = {
    "report_pdf": "Report (PDF)", "stats_xlsx": "Statistics workbook (Excel)",
    "accessions": "Assigned accessions (TSV)", "submission_xml": "submission.xml (NCBI)",
    "report_xml": "NCBI report.xml", "sra_xlsx": "SRA submission workbook (Excel)",
    "sra_metadata": "SRA run metadata (TSV)", "biosample_attrs": "BioSample attributes (TSV)",
    "genbank_fasta": "GenBank concatenated FASTA", "genbank_source": "GenBank source modifiers (TSV)",
    "crosswalk": "NCBI existence crosswalk (TSV)", "duplicate_mask": "Duplicate-upload mask (TSV)",
    "excluded": "Excluded/flagged samples report", "qc": "QC verdicts (JSON)",
    "run_manifest": "Run manifest / provenance (JSON)", "log": "Pipeline log",
}


def _result_label(rel: str, category: Optional[str]) -> str:
    return _CATEGORY_LABEL.get(category, rel)


def _collect_result_files(run_dir: Path, include_all: bool) -> List[Dict]:
    files: List[Dict] = []
    if not run_dir.is_dir():
        return files
    for p in sorted(run_dir.rglob("*")):
        if not p.is_file() or p.name.endswith(".log"):
            continue
        rel = str(p.relative_to(run_dir))
        category = _result_category(rel)
        if not include_all and category is None:
            continue
        try:
            stat = p.stat()
        except OSError:
            continue
        files.append({"name": rel, "path": str(p), "label": _result_label(rel, category),
                      "size": stat.st_size, "openable": _can_open_inline(rel), "category": category})

    def sort_key(f):
        c = f.get("category")
        return (_CATEGORY_ORDER.get(c, 50), f["name"])

    files.sort(key=sort_key)
    for f in files:
        if include_all and f.get("category") is None:
            f["label"] = f["name"]
    return files


@app.get("/api/projects/{name}/file")
def api_project_file(name: str, path: str = Query(...), inline: int = 0):
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    root = project_dir.resolve()
    target = Path(path).resolve()
    if root != target and root not in target.parents:
        raise HTTPException(403, "Path outside project directory")
    if not target.is_file():
        raise HTTPException(404, f"File not found: {path}")
    want_inline = bool(inline) and _can_open_inline(target.name)
    disposition = "inline" if want_inline else "attachment"
    return FileResponse(target, media_type=_media_type_for(target.name),
                        headers={"Content-Disposition": f'{disposition}; filename="{target.name}"'})


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
@app.get("/api/jobs")
def api_list_jobs():
    return JSONResponse(job_manager.list_jobs())


@app.get("/api/jobs/{job_id}")
def api_get_job(job_id: str):
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return JSONResponse(job)


@app.get("/api/jobs/{job_id}/log")
async def api_job_log(job_id: str, request: Request):
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    log_path = Path(job["log_path"])
    _ansi_re = re.compile(r'\x1b\[[0-9;]*[mGKHFABCDJsur]')

    async def event_stream():
        position = 0
        while True:
            if await request.is_disconnected():
                break
            current_job = job_manager.get_job(job_id)
            if log_path.exists():
                async with aiofiles.open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    await f.seek(position)
                    chunk = await f.read(4096)
                    if chunk:
                        for line in chunk.splitlines(keepends=True):
                            clean = _ansi_re.sub("", line.rstrip())
                            if clean:
                                yield f"data: {clean}\n\n"
                        position += len(chunk.encode("utf-8"))
            if current_job and current_job["status"] in ("succeeded", "failed"):
                yield "data: [DONE]\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/jobs/{job_id}/results")
def api_job_results(job_id: str, all: int = Query(0)):
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    files = []
    cwd = job.get("cwd")
    if cwd and Path(cwd).is_dir():
        files = _collect_result_files(Path(cwd), bool(all))
    log_path = Path(job.get("log_path", ""))
    if log_path.is_file():
        files.append({"name": "pipeline_log.txt", "label": "Pipeline log",
                      "size": log_path.stat().st_size, "openable": True, "category": "log", "is_log": True})
    return JSONResponse(files)


@app.get("/api/jobs/{job_id}/file")
def api_job_file(job_id: str, path: str = Query(...), inline: int = 0):
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if path == "pipeline_log.txt":
        target = Path(job.get("log_path", ""))
        display_name = f"{job_id[:8]}_pipeline_log.txt"
    else:
        cwd = job.get("cwd")
        if not cwd:
            raise HTTPException(404, "No run directory for job")
        run_dir = Path(cwd).resolve()
        target = (run_dir / path).resolve()
        if run_dir != target and run_dir not in target.parents:
            raise HTTPException(403, "Path outside run directory")
        display_name = target.name
    if not target.is_file():
        raise HTTPException(404, f"File not found: {path}")
    want_inline = bool(inline) and _can_open_inline(target.name)
    disposition = "inline" if want_inline else "attachment"
    return FileResponse(target, media_type=_media_type_for(target.name),
                        headers={"Content-Disposition": f'{disposition}; filename="{display_name}"'})


# ---------------------------------------------------------------------------
# Static frontend — must be last
# ---------------------------------------------------------------------------
if _FRONTEND_DIST.is_dir():
    _INDEX_HTML = _FRONTEND_DIST / "index.html"

    @app.get("/")
    def index():
        return FileResponse(_INDEX_HTML, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="static")
else:
    @app.get("/")
    def root():
        return JSONResponse({"error": "Frontend not built. Run: cd frontend && npm run build"}, status_code=503)
