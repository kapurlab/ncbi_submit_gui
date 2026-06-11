"""
Read the per-user config (and env overrides) from the pipeline subprocess.

The backend (backend/app/config.py) owns the config file; the pipeline runs as a
separate process and only needs to read it, so this avoids importing the web
app. Returns the submission contact block and the FTP credentials. Environment
variables win over the stored file (so a launcher can inject secrets without
writing them to disk).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Tuple


def _config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg else (Path.home() / ".config")
    return base / "ncbi_submit_gui" / "config.json"


def _load() -> Dict[str, str]:
    try:
        return json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_contact_and_creds() -> Tuple[Dict[str, str], Dict[str, str]]:
    cfg = _load()

    def val(key: str, env: str, default: str = "") -> str:
        return (os.environ.get(env) or cfg.get(key) or default).strip()

    email = val("ncbi_email", "NCBI_EMAIL")
    contact = {
        "email": email,
        "organization": val("ncbi_organization", "NCBI_ORGANIZATION", "Kapur Laboratory"),
        "first_name": val("ncbi_contact_first", "NCBI_CONTACT_FIRST"),
        "last_name": val("ncbi_contact_last", "NCBI_CONTACT_LAST"),
        "spuid_namespace": val("ncbi_spuid_namespace", "NCBI_SPUID_NAMESPACE", "ncbi_submit_gui"),
    }
    creds = {
        "ftp_host": val("ncbi_ftp_host", "NCBI_FTP_HOST", "ftp-private.ncbi.nlm.nih.gov"),
        "ftp_user": val("ncbi_ftp_user", "NCBI_FTP_USER"),
        "ftp_pass": val("ncbi_ftp_pass", "NCBI_FTP_PASS"),
    }
    return contact, creds
