"""
Per-user configuration for the NCBI Submit GUI.

Stored under XDG config (~/.config/ncbi_submit_gui/config.json). Holds the
projects roots and the NCBI credentials needed for eutils lookups and
programmatic FTP submission.

SECURITY: credential values (API key, FTP password) live ONLY in this per-user
file or in environment variables — never in the repo. The /api/config endpoint
returns booleans for whether each credential is set, never the secret itself.
Environment variables take precedence over the stored config so an OOD launcher
or a CI run can inject them without writing them to disk.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional


def _user_config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg) / "ncbi_submit_gui"
    return Path.home() / ".config" / "ncbi_submit_gui"


DATA_DIR = _user_config_dir()
CONFIG_PATH = DATA_DIR / "config.json"

# The multi-user "shared projects" root, if this deployment has one. A laptop
# normally does not — only a lab server or an OOD site. Never assume a path:
# probe in order of authority and fall back to None (no shared root) rather than
# a fictional one, so macOS/WSL users aren't shown a directory that cannot exist.
#   1. BDTOOLS_SHARED_PROJECTS_ROOT — exported by the launcher, which resolved it
#      from the machine's recorded site config. An explicitly empty value is
#      authoritative: it DISABLES the shared root.
#   2. the user's own `shared_projects_root` setting — see shared_projects_root()
# There is no step 3. A site supplies its own value (bdtools records it in
# <BDTOOLS_HOME>/site.conf); this file contains no path of its own, so the same
# release is correct on macOS, WSL, Linux and OOD without editing.
#
# This used to be one lab server's projects path, guarded by is_dir(). The guard
# kept the value out of config.json off that server, but the literal still
# decided what "shared" MEANT: any other site with its own shared root got no
# shared projects at all, silently, because the only path this file would accept
# was one it could never have.
_ENV_SHARED_PROJECTS_ROOT = "BDTOOLS_SHARED_PROJECTS_ROOT"


def _default_shared_projects_root() -> str:
    env = os.environ.get(_ENV_SHARED_PROJECTS_ROOT)
    return env.strip() if env is not None else ""


_DEFAULT_SHARED_PROJECTS_ROOT = _default_shared_projects_root()


def shared_projects_root() -> Optional[Path]:
    """The resolved shared-projects root, or None when this deployment has none.

    Read through this rather than a module constant, so the Settings value is
    honoured: main.py used to carry its own hard-coded literal, which meant
    setting `shared_projects_root` in the GUI changed what Settings displayed and
    nothing about where projects were discovered.

    Returns None — never Path("") — because Path("") is Path("."), the current
    working directory. An "unset" sentinel that silently means "look in ." would
    turn a missing shared root into project lookups against wherever uvicorn
    happens to have been started."""
    env = os.environ.get(_ENV_SHARED_PROJECTS_ROOT)
    if env is not None:
        return Path(env.strip()) if env.strip() else None
    try:
        configured = str(load_config().get("shared_projects_root", "") or "").strip()
    except Exception:
        configured = ""
    if configured:
        return Path(configured)
    return Path(_DEFAULT_SHARED_PROJECTS_ROOT) if _DEFAULT_SHARED_PROJECTS_ROOT else None

# Credential keys that must never be echoed back to the browser. The config API
# reports presence (a boolean) for each instead of the value.
SECRET_KEYS = ("ncbi_api_key", "ncbi_ftp_pass")

# Environment-variable overrides, checked on every load so a launcher can inject
# credentials without persisting them.
_ENV_OVERRIDES = {
    "ncbi_email": "NCBI_EMAIL",
    "ncbi_api_key": "NCBI_API_KEY",
    "ncbi_ftp_host": "NCBI_FTP_HOST",
    "ncbi_ftp_user": "NCBI_FTP_USER",
    "ncbi_ftp_pass": "NCBI_FTP_PASS",
    "submit_target": "NCBI_SUBMIT_TARGET",
}

DEFAULTS: Dict[str, Any] = {
    "projects_root": str(Path.home() / "projects"),
    "shared_projects_root": _DEFAULT_SHARED_PROJECTS_ROOT,
    "saved_project_roots": [],
    # NCBI eutils — used for the "already in NCBI?" existence checks and the
    # SRA<->GenBank BioSample crosswalk. An API key lifts the rate limit 3->10/s.
    "ncbi_email": "",
    "ncbi_api_key": "",
    # Programmatic submission FTP account (request via gb-admin@ncbi.nlm.nih.gov).
    # Default host is NCBI's submission FTP; submissions land under a Test/ or
    # Production/ subfolder chosen by `submit_target`.
    "ncbi_ftp_host": "ftp-private.ncbi.nlm.nih.gov",
    "ncbi_ftp_user": "",
    "ncbi_ftp_pass": "",
    # 'test' (safe default) routes to the NCBI test area; 'prod' is real.
    "submit_target": "test",
    # Default organism preset (config/organisms/<name>.yaml).
    "organism_preset": "generic",
}


def load_config() -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        save_config({k: v for k, v in DEFAULTS.items()})
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    # Environment variables win over the stored config (never written back).
    for key, env_name in _ENV_OVERRIDES.items():
        val = os.environ.get(env_name)
        if val:
            cfg[key] = val.strip()
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # The file may hold secrets — keep it private to the user.
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def public_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """A copy safe to send to the browser: secret values replaced by a
    `<key>_set` boolean so the UI can show 'configured' without leaking them."""
    out: Dict[str, Any] = {}
    for k, v in cfg.items():
        if k in SECRET_KEYS:
            out[f"{k}_set"] = bool(str(v).strip())
        else:
            out[k] = v
    return out
