# Confluence → Markdown / Obsidian migrator

A **free-tool** pipeline that migrates a Confluence Cloud instance into a
Git-backed Markdown knowledge base (Obsidian-compatible) with **locally editable
draw.io diagrams** and **no vendor lock-in**.

It orchestrates:

- **[`cme`](https://github.com/Spenhouet/confluence-markdown-exporter)** (MIT) — Markdown conversion engine
- **Confluence REST API** — space enumeration, attachment inventory + download
- **draw.io Desktop CLI** (Apache-2.0) — `mxfile → .drawio.svg` (editable)
- **Git + Git LFS** — versioning + large binaries

## Documentation

| Doc | What it is |
|-----|------------|
| [docs/concept.md](docs/concept.md) | High-level, vendor-neutral migration concept |
| [docs/concept.svg](docs/concept.svg) | Architecture graphic |
| [docs/plan.md](docs/plan.md) | Full technical plan (toolchain, commands, phasing, runbook) |

## Repository layout

```
.
├── README.md              # this file
├── requirements.txt       # Python deps (requests, python-dotenv, PyYAML)
├── .env.example           # secrets template (copy to .env)
├── config.yml             # export structure & policy (committed, no secrets)
├── config.yml.example     # documented config template
├── docs/                  # concept + plan + architecture graphic
├── migrator/              # the CLI tool (python -m migrator)
│   ├── __main__.py        # CLI entrypoint + run-dir handling
│   ├── config.py          # layered .env + config.yml -> Config
│   ├── settings.py        # typed config.yml model + policy helpers
│   ├── confluence.py      # REST client (spaces, CQL, attachments, download)
│   ├── exporter.py        # cme wrapper (derives cme config from config.yml)
│   ├── diagrams.py        # mxfile -> .drawio.svg + page ref rewrite (parallel/cached)
│   ├── links.py           # page-id map + internal/tiny link + anchor rewrite
│   ├── normalize.py       # frontmatter normalization to the target schema
│   ├── index.py           # _index.md folder-note generation
│   ├── anonymize.py       # optional author/email redaction
│   ├── gitops.py          # git/LFS/Obsidian scaffolding + initial commit
│   ├── report.py          # count reconciliation + QA scans -> migration_report.md
│   └── utils.py
├── tests/                 # pytest unit tests (pip install -r requirements-dev.txt)
└── export/                # git-ignored; timestamped run output lives here
```

## Requirements (all free)

```bash
pipx install confluence-markdown-exporter   # the `cme` engine
brew install --cask drawio                  # draw.io Desktop (or apt/AUR on Linux)
brew install git git-lfs && git lfs install
python3 -m pip install -r requirements.txt  # requests, python-dotenv, PyYAML
```

## Configure

Two files, clean split:

- **`.env`** (git-ignored) — secrets/connection only: `CONFLUENCE_URL` (incl.
  `/wiki`), `CONFLUENCE_USERNAME`, `CONFLUENCE_API_TOKEN`.
- **`config.yml`** (committed) — all structure & policy.

```bash
cp .env.example .env              # fill in the 3 credential lines
# config.yml ships with sensible defaults; cp config.yml.example config.yml to reset
```

API token: <https://id.atlassian.com/manage-profile/security/api-tokens>. Use an
account that can read **all** in-scope spaces (admin scope for archived/personal).

> One-time: run `cme config` so the exporter has its own auth/output settings.

### What `config.yml` controls

| Section | Controls |
|---------|----------|
| `export` | run-dir root, `run_id` format, `latest` symlink, folder layout, hierarchy mirroring, index files |
| `scope` | which spaces (include/exclude, types, archived, personal); blogposts/comments |
| `attachments` | download mode, allow/deny extensions + MIME types, max size, temp-skip, filename template |
| `diagrams` | enable, source media type, output format (`drawio_svg`/`drawio_png`/`keep_mxfile`), embed XML, keep mxfile |
| `markdown` | frontmatter fields, callouts, page-properties report mode, includes, link style (`relative`/`wikilink`) |
| `git` | git init, LFS on/off, LFS extensions, Obsidian config, initial commit + message |
| `git.obsidian_metadata` | Obsidian Properties presets (`types.json`) and imported-page template scaffolding |
| `anonymize` | enable, redact emails, pseudonymize authors, author fields, extra redact patterns |
| `runtime` | draw.io binary, HTTP timeout, retries, incremental, parallel workers |

See [config.yml.example](config.yml.example) for every option documented inline.

## Runs (timestamped output)

Every invocation targets a run directory `export/<run_id>/` (default `run_id` is
a timestamp; `export/` is git-ignored). `export/latest` points at the newest run.

- Producer commands (`export`, `inventory`, `all`) create a **new** run unless
  you pass `--run-id`.
- Consumer commands (`diagrams`, `links`, `scaffold`, `report`) default to the
  **latest** run; pin one with `--run-id <id>`.

Global flags: `--env <path>` (default `.env`), `--config <path>` (default
`config.yml`), `--run-id <id>`. Flags go **before** the subcommand.

## Usage

```bash
python -m migrator preflight     # read-only gap analysis BEFORE migrating
python -m migrator preflight-report  # render HTML visualizer for all preflight runs
python -m migrator export-dashboard  # render HTML dashboard for export runs
python -m migrator spaces        # list in-scope spaces (current + archived)
python -m migrator export        # cme org export (config derived from config.yml)
python -m migrator inventory     # _meta/attachments_inventory.csv (policy + referenced)
python -m migrator diagrams      # download mxfiles -> .drawio.svg + rewrite page refs
python -m migrator links         # page-id map + rewrite internal/tiny links + anchors
python -m migrator normalize     # normalize frontmatter to the configured schema
python -m migrator index         # generate _index.md folder notes
python -m migrator anonymize     # strip/pseudonymize authors + redact (if enabled)
python -m migrator scaffold      # .gitattributes/.gitignore/.obsidian + git init/commit
python -m migrator report        # reconcile + QA scans -> migration_report.md/html

python -m migrator all           # run the whole pipeline end to end
python -m migrator --run-id 20260619-111200 diagrams   # target a specific run
python -m migrator --dry-run all # preview every step without writing/downloading
python -m migrator -v export     # verbose (debug) logging; -q for quiet
```

Global flags `--dry-run`, `-v/--verbose`, and `-q/--quiet` work with every
subcommand and go **before** the subcommand.

### Preflight (gap analysis)

Run `preflight` first to surface problems before the real migration. It is
**read-only** and grades each check `PASS` / `WARN` / `FAIL`:

- **Connectivity & Auth**: instance reachable, token valid, who the token is,
  whether archived/restricted content is visible (under-scoped token warning).
- **Tooling**: `cme`, draw.io binary, `git`/`git-lfs`, free disk space.
- **Config**: scope resolves to >= 1 space, allow/deny consistency, LFS/diagram settings.
- **Scope & Volume**: in-scope spaces by type/status, page/blogpost/comment counts.
- **Attachments & Diagrams**: media-type tally, policy-skipped + oversized counts,
  mxfile/temp counts, Git LFS size estimate.
- **Content & Macros** (sampled): macro inventory classified clean / lossy / unknown,
  macro-only/empty-body pages.
- **Naming & Links**: duplicate, overlong, and non-ASCII/illegal page titles.

```bash
python -m migrator preflight                  # sample 100 page bodies for macros
python -m migrator preflight --full           # scan every page body
python -m migrator preflight --sample-pages 300
python -m migrator preflight --strict         # exit 2 on FAIL, 1 on WARN (CI gate)
```

Results are written to `export/<run_id>/preflight_report.md`,
`export/<run_id>/_meta/preflight.json`, and a self-contained
`export/<run_id>/preflight_report.html`.

To browse every run visually, render an aggregate dashboard (read-only, no
network -- it just reads the existing `preflight.json` files):

```bash
python -m migrator preflight-report           # builds export/preflight-dashboard.html
python -m migrator preflight-report --open     # also open it in your browser
```

The dashboard lists each run with its verdict badge and PASS/WARN/FAIL counts
and links to the per-run HTML page (graded checklist with the full macro lists).

### Export dashboard & checkpoints

Every run step writes checkpoint state to
`export/<run_id>/_meta/run_manifest.json`, including step status, timestamps,
duration, outputs, and failures. This makes interrupted or resumed runs easier
to inspect.

Render the export visualizer across all runs:

```bash
python -m migrator export-dashboard
python -m migrator export-dashboard --open
```

The dashboard is self-contained HTML at `export/export-dashboard.html`. It shows
per-run step history, checkpoint events, Markdown/assets/diagram counts,
inventory/page/diagram map counts, QA findings, and browseable links to generated
files.

## Output layout (per run)

```
export/<run_id>/
  <Space>/<Page>.md
  <Space>/assets/<fileId>.<ext>
  <Space>/diagrams/<name>.drawio.svg
  _meta/attachments_inventory.csv
  _meta/pageid_map.csv
  _meta/run_manifest.json
  migration_report.md
  migration_report.html
  .gitattributes  .gitignore  .obsidian/
  Templates/Confluence page.md
export/latest -> <run_id>
```

Folder names, hierarchy mirroring, allowed file types, LFS extensions, diagram
format, and link style are all configurable in `config.yml`.

## Notes

- **Non-destructive**: only read operations hit Confluence; the source stays
  intact until you decide to make it read-only.
- **Editor-agnostic**: the vault is plain Markdown + Git, so it also opens in
  VS Code/VSCodium (Foam/Dendron) or Logseq — no lock-in.
- **Diagrams**: `.drawio.svg` renders as an image *and* reopens in draw.io
  (Desktop or the Obsidian draw.io plugin) for editing.
- **Reports**: `report` writes both Markdown and rich self-contained HTML.
- **Obsidian metadata**: scaffold writes `.obsidian/types.json` plus a starter
  imported-page template when `git.obsidian_metadata.enabled` is true.
- **Free tools only**: Obsidian is free for commercial use (since 2025); the
  only cost to watch is hosted Git LFS storage quotas — see [docs/plan.md](docs/plan.md).