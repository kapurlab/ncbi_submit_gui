"""
Load the config-driven organism presets, BioSample packages, and standards.

Keeping organism logic (gene maps, fixed fields, column aliases) in YAML rather
than code is what makes this tool general/multi-pathogen and easy to deploy to
other sites: adding a pathogen is a new file in config/organisms/, not a code
change.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _REPO_ROOT / "config"
_ORGANISMS_DIR = _CONFIG_DIR / "organisms"
_PACKAGES_FILE = _CONFIG_DIR / "ncbi" / "biosample_packages.yaml"
_STANDARDS_FILE = _CONFIG_DIR / "standards.yaml"


def _load_yaml(path: Path) -> Dict[str, Any]:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def list_presets() -> List[Dict[str, str]]:
    """Return [{name, display_name}] for every organism preset on disk."""
    out: List[Dict[str, str]] = []
    if not _ORGANISMS_DIR.is_dir():
        return out
    for f in sorted(_ORGANISMS_DIR.glob("*.yaml")):
        data = _load_yaml(f)
        name = data.get("name") or f.stem
        out.append({"name": name, "display_name": data.get("display_name", name)})
    return out


def load_preset(name: str) -> Dict[str, Any]:
    """Load one organism preset by name, falling back to 'generic'."""
    candidate = _ORGANISMS_DIR / f"{name}.yaml"
    if not candidate.is_file():
        candidate = _ORGANISMS_DIR / "generic.yaml"
    data = _load_yaml(candidate)
    if not data:
        raise FileNotFoundError(f"No organism preset found for {name!r} (and no generic.yaml)")
    data.setdefault("sra", {})
    data.setdefault("genbank", {})
    data.setdefault("gene_map", [])
    data.setdefault("column_aliases", {})
    return data


def load_packages() -> Dict[str, Any]:
    return _load_yaml(_PACKAGES_FILE)


def load_standards() -> Dict[str, Any]:
    return _load_yaml(_STANDARDS_FILE)


def normalize_header(h: str) -> str:
    """Canonicalize a column header: lowercase, collapse non-alphanumerics to _."""
    return re.sub(r"[^a-z0-9]+", "_", str(h).strip().lower()).strip("_")


def build_alias_index(preset: Dict[str, Any]) -> Dict[str, str]:
    """Map every normalized accepted header -> its canonical field name."""
    index: Dict[str, str] = {}
    for canonical, aliases in (preset.get("column_aliases") or {}).items():
        for alias in aliases:
            index[normalize_header(alias)] = canonical
    return index
