#!/usr/bin/env bash
# Register this tool's OOD batch_connect apps on the dashboard.
# Must run as root (the apps dir is root-owned), e.g.:  sudo deploy/register_ood_apps.sh
set -euo pipefail
SRC=/srv/kapurlab/tools/ncbi_submit_gui/ood/apps
DST=/var/www/ood/apps/sys
for app in ncbi_submit_gui ncbi_submit_gui_dev; do
  echo "Installing $app -> $DST/$app"
  rm -rf "$DST/$app"
  cp -a "$SRC/$app" "$DST/$app"
  chmod -R go+rX "$DST/$app"
done
echo "Done. The apps appear under Bioinformatics in the OOD dashboard."
echo "NOTE: the curated 'Kapur Lab Pipelines' landing page is hand-edited — add the card there manually."
