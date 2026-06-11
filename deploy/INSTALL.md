# Install & deploy — NCBI Submit GUI

## 1. Environment + frontend (no sudo)

```bash
cd /srv/kapurlab/tools/ncbi_submit_gui
deploy/install.sh --conda-base /srv/kapurlab/tools/miniforge3      # preview: --dry-run
```

Creates the shared conda env at `env/` (personal fallback `~/miniforge3/envs/ncbi_submit`),
`pip install`s the backend requirements, verifies `seqkit` + `table2asn`, and
builds `frontend/dist/`. No large database download is required.

## 2. Register the OOD apps (root)

```bash
sudo deploy/register_ood_apps.sh
```

Copies `ood/apps/ncbi_submit_gui` and `ncbi_submit_gui_dev` into
`/var/www/ood/apps/sys/`. They appear under **Interactive Apps → Bioinformatics**.

The curated **"Kapur Lab Pipelines"** landing page is hand-edited — add the
**NCBI Submit** card there manually (same as the sibling tools).

## 3. Credentials

Set per user, in the GUI **Settings** panel (stored at
`~/.config/ncbi_submit_gui/config.json`, chmod 600) or via environment:

| Purpose | Config key | Env var |
|---|---|---|
| E-utilities contact email | `ncbi_email` | `NCBI_EMAIL` |
| E-utilities API key (raises rate limit) | `ncbi_api_key` | `NCBI_API_KEY` |
| Submission FTP host | `ncbi_ftp_host` | `NCBI_FTP_HOST` |
| Submission FTP user | `ncbi_ftp_user` | `NCBI_FTP_USER` |
| Submission FTP password | `ncbi_ftp_pass` | `NCBI_FTP_PASS` |

The **programmatic-submission FTP account** is requested from NCBI via
gb-admin@ncbi.nlm.nih.gov; it is separate from the E-utilities API key. Until it
is configured, the tool runs in prep / `--dry-run` mode (everything except the
upload).

## 4. Push to GitHub (for the dev branch-picker app)

The `_dev` app checks branches out from `origin`, so push the repo:

```bash
# create the repo at github.com/kapurlab/ncbi_submit_gui in the web UI, then:
git push -u origin feature/initial-build
```

The production card serves the committed on-disk `dist/` and does not require the push.

## Reloads

- `bin/` scripts → next run · `backend/app/` → new session (or dev `--reload`)
- `frontend/src` → `npm run build` + new session · `ood/**` → re-run `register_ood_apps.sh`
