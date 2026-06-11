"""CLI: regenerate the stats workbook + PDF for an existing run dir.

    python -m reporting --outdir <dir> --label <name> [--date <stamp>]
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

from . import build

ap = argparse.ArgumentParser(description="Build NCBI Submit stats.xlsx + report.pdf for a run dir.")
ap.add_argument("--outdir", type=Path, required=True)
ap.add_argument("--label", default="ncbi_submit")
ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))
args = ap.parse_args()
print(json.dumps(build(args.outdir, args.label, args.date), indent=2))
