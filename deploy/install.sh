#!/usr/bin/env bash
# install.sh — idempotent, no-sudo deployment of the NCBI Submit GUI.
#
# Mirrors the amr_plus_gui / kraken sandbox pattern. Every heavy step is
# skippable and clearly logged. Safe to re-run.
#
# What it does:
#   1. Locate/create the conda env (shared at <repo>/env, else personal ncbi_submit).
#   2. pip install backend/requirements.txt into that env.
#   3. Verify seqkit + table2asn are on PATH (no large DB to download).
#   4. Build the React frontend (frontend/dist/).
#
# Usage:
#   deploy/install.sh [--personal] [--conda-base DIR] [--skip-frontend] [--dry-run]
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SHARED_ENV="${REPO_DIR}/env"
PERSONAL_ENV_NAME="ncbi_submit"
CONDA_BASE="/srv/kapurlab/tools/miniforge3"
USE_PERSONAL=0
SKIP_FRONTEND=0
DRY_RUN=0

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR\033[0m %s\n' "$*" >&2; exit 1; }
run()  { if [[ ${DRY_RUN} -eq 1 ]]; then echo "  [dry-run] $*"; else "$@"; fi; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --personal)      USE_PERSONAL=1; shift;;
    --conda-base)    CONDA_BASE="$2"; shift 2;;
    --skip-frontend) SKIP_FRONTEND=1; shift;;
    --dry-run)       DRY_RUN=1; shift;;
    -h|--help)       sed -n '2,16p' "$0"; exit 0;;
    *) die "unknown arg: $1";;
  esac
done

log "NCBI Submit GUI install"
echo "  repo:  ${REPO_DIR}"
[[ ${DRY_RUN} -eq 1 ]] && warn "DRY RUN — no changes will be made"

# ---------------------------------------------------------------------------
# 1. conda env
# ---------------------------------------------------------------------------
CONDA="${CONDA_BASE}/bin/conda"
[[ -x "${CONDA}" ]] || CONDA="$(command -v conda 2>/dev/null || true)"
[[ -n "${CONDA}" && -x "${CONDA}" ]] || die "conda not found. Install miniforge to ${CONDA_BASE} or pass --conda-base."
ok "conda: ${CONDA}"

CONDA_FRONTEND="${CONDA_FRONTEND:-}"
if [[ -z "${CONDA_FRONTEND}" ]]; then
  if [[ -x "${CONDA_BASE}/bin/mamba" ]]; then CONDA_FRONTEND="${CONDA_BASE}/bin/mamba"
  elif command -v mamba >/dev/null 2>&1; then CONDA_FRONTEND="$(command -v mamba)"
  else CONDA_FRONTEND="${CONDA}"; fi
fi
ok "env builder: ${CONDA_FRONTEND}"

ENV_FILE="${REPO_DIR}/conda_setup/environment.yml"
if [[ ${USE_PERSONAL} -eq 1 ]]; then
  ENV_BIN="$("${CONDA}" run -n "${PERSONAL_ENV_NAME}" sh -c 'echo $CONDA_PREFIX/bin' 2>/dev/null || true)"
  ENV_DESC="personal env ${PERSONAL_ENV_NAME}"
  ENV_EXISTS=$("${CONDA}" env list | awk '{print $1}' | grep -qx "${PERSONAL_ENV_NAME}" && echo 1 || echo 0)
  CREATE_FLAG=("-n" "${PERSONAL_ENV_NAME}")
else
  ENV_BIN="${SHARED_ENV}/bin"
  ENV_DESC="shared env ${SHARED_ENV}"
  ENV_EXISTS=$([[ -x "${SHARED_ENV}/bin/python" ]] && echo 1 || echo 0)
  CREATE_FLAG=("-p" "${SHARED_ENV}")
fi

if [[ "${ENV_EXISTS}" -eq 1 ]]; then
  ok "${ENV_DESC} already exists — skipping create"
else
  # A cancelled solve leaves a partial env dir with no python; clear it first.
  if [[ ${USE_PERSONAL} -eq 0 && -d "${SHARED_ENV}" ]]; then
    warn "removing incomplete env at ${SHARED_ENV} (no python found)"
    run rm -rf "${SHARED_ENV}"
  fi
  log "creating ${ENV_DESC} from ${ENV_FILE} (solve can take 1-3 min)"
  run "${CONDA_FRONTEND}" env create "${CREATE_FLAG[@]}" -f "${ENV_FILE}"
fi

# A --personal env may have just been created above; if so, the ENV_BIN probed
# earlier (via `conda run` before the env existed) is empty, which would make
# PYTHON="/python". Re-resolve now that the env exists — prefer the live prefix,
# fall back to <conda base>/envs/<name> (where `conda env create -n` puts it).
if [[ ${USE_PERSONAL} -eq 1 && ! -x "${ENV_BIN}/python" ]]; then
  ENV_BIN="$("${CONDA}" run -n "${PERSONAL_ENV_NAME}" sh -c 'echo $CONDA_PREFIX/bin' 2>/dev/null || true)"
  [[ -x "${ENV_BIN}/python" ]] || ENV_BIN="$("${CONDA}" info --base 2>/dev/null)/envs/${PERSONAL_ENV_NAME}/bin"
fi
PYTHON="${ENV_BIN}/python"
[[ ${DRY_RUN} -eq 1 || -x "${PYTHON}" ]] || die "env python not found at '${PYTHON}' — ${ENV_DESC} did not build correctly."
# Put the env's bin on PATH for every tool call below. table2asn/seqkit and the
# OOD session set PATH the same way.
if [[ -d "${ENV_BIN}" ]]; then export PATH="${ENV_BIN}:${PATH}"; fi
log "pip install backend requirements into ${ENV_DESC}"
run "${PYTHON}" -m pip install -r "${REPO_DIR}/backend/requirements.txt"

# ---------------------------------------------------------------------------
# 2. Verify sequence tools (no DB downloads needed)
# ---------------------------------------------------------------------------
if command -v seqkit >/dev/null 2>&1; then ok "seqkit: $(seqkit version 2>&1 | head -1)"
else warn "seqkit not on PATH — input QC metrics will be unavailable at runtime."; fi
if command -v table2asn >/dev/null 2>&1; then ok "table2asn: $(table2asn -version 2>&1 | head -1)"
else warn "table2asn not on PATH — GenBank .sqn build will be skipped (FASTA + source table still produced)."; fi

# ---------------------------------------------------------------------------
# 3. Frontend build
# ---------------------------------------------------------------------------
if [[ ${SKIP_FRONTEND} -eq 1 ]]; then
  warn "skipping frontend build (--skip-frontend)"
else
  log "building React frontend"
  pushd "${REPO_DIR}/frontend" >/dev/null
  if command -v npm >/dev/null 2>&1; then
    run npm ci || run npm install
    run npm run build
  elif [[ -x node_modules/.bin/vite ]]; then
    run node_modules/.bin/vite build
  else
    SIB="/srv/kapurlab/tools/amr_plus_gui/frontend/node_modules"
    if [[ -d "${SIB}" && ! -e node_modules ]]; then
      run ln -s "${SIB}" node_modules
      run node_modules/.bin/vite build
    else
      warn "no npm and no node_modules — frontend not built. Install Node and re-run."
    fi
  fi
  popd >/dev/null
  [[ -f "${REPO_DIR}/frontend/dist/index.html" ]] && ok "frontend built: ${REPO_DIR}/frontend/dist/"
fi

log "Done. Register the OOD app (sudo deploy/register_ood_apps.sh) and launch a session."
echo "  Backend entry:  ${REPO_DIR}/backend/app/main.py (uvicorn app.main:app)"
echo "  Env python:     ${PYTHON}"
echo "  Credentials:    set NCBI email/API key + submission FTP creds in the GUI Settings."
