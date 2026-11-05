# CatC_Migrate_Maps

Interactive Python tool to migrate Catalyst Center site maps (buildings,
floors, AP placement, RF calibration) from one Catalyst Center system to
another, one site at a time.

Built for repeated use as new areas are rolled out — run it again for
each area, it is not a one-off migration script.

## What it does

`python3 dnac_map_migrate.py` runs the whole migration for one site at a time:

1. Takes hosts and credentials from `.env` (or prompts for them).
2. Lists the site hierarchy on the **old** system; you pick the area,
   building or floor to migrate (everything underneath it comes too).
3. Asks for the **destination path** on the new system (Enter = same path).
4. Checks every site under the selection **by full path** on the new
   system and reports `[OK]`, `[CREATE]` (floor missing but its building
   exists) or `[MISSING]` (area/building missing).
5. Exports the map archive (floor plans, dimensions) from the old system.
6. **Removes AP placements** from the archive: the old APs are being
   replaced, so placing them on the new system is pointless. Floor plans
   are kept. Set `DNAC_KEEP_APS=true` to keep them.
7. If the destination path differs, **remaps** the archive's hierarchy.
8. **Creates missing floors** on the new system (after a Y/n prompt) using
   the size and level from the archive, then uploads and shows a readable
   pre-import preview.
9. Requires an explicit `yes` (import replaces map data for matched
   sites), commits, waits for completion and prints the final result,
   including any per-entity errors.
10. Loops for the next site.


## Requirements

- Python 3.8+
- Network access to both Catalyst Center systems (HTTPS)
- An account with API access on both systems (same username/password is
  assumed for both)

```bash
pip install -r requirements.txt
```

## Usage

```bash
python3 dnac_map_migrate.py
```

You'll be prompted for:

- Old system host/IP (source)
- New system host/IP (destination)
- Username (used for both systems)
- Password (used for both systems, hidden input)

Then pick a site from the old system's hierarchy, review the name
cross-check against the new system, confirm the pre-import preview, and
repeat for the next area as needed.

## Verified behaviour (tested against a lab Catalyst Center)

- **Export request:** `POST /maps/export/{id}` answers 415 to every
  `application/json` request. It accepts `Content-Type: text/plain`; the body
  is used as the archive file name (plain alphanumerics only). The SDK's
  `export_map_archive` cannot make this call (its schema is `string`, its
  default body `{}` is rejected), so the script posts it directly.
- **Export result:** the finished task has `data: "/file/<fileId>"`; the tar.gz
  is downloaded with the SDK file API. It contains `xmlDir/MapsImportExport.xml`,
  `images/*` (floor plans) and `metadata.txt`.
- **Planned APs** are exported with position and radio settings.
  `metadata.txt` "Count of APs" counts only real (assigned) APs.
  Placement of real APs has not been tested.
- **Import upload:** `POST /maps/import/start` multipart field is `archive`
  (`file` returns 400).
- **Matching is by full hierarchy path** stored in the XML as nested
  `Site > Building > Floor` names (without `Global`), not by leaf name alone.
- **Import cannot create areas, buildings or floors.** Anything missing on the
  target is reported "doesn't exist ... will be ignored". The tool creates
  missing **floors** itself (Create Site API) when their building exists;
  areas and buildings must be created first, with exact names.
- **Managed APs already on a floor:** the preview says `Managed AP '...' will be removed` and the
  result says `got removed`. Tested on one live floor (1 AP): this clears the AP from the map only;
  its site assignment, provisioning and reachability were unchanged. Tested on a single AP only, so
  canary a floor with several APs before a bulk run.
- **Remap:** `--remap` rewrites the XML nesting so an exported subtree lands
  on a different existing path. Preview confirmed on an existing floor;
  a committed import has not yet been tested.

```bash
python3 dnac_map_migrate.py --export-only                    # uses .env
python3 dnac_map_migrate.py --remap IN.tar.gz "Global/old/path" "Global/new/path" OUT.tar.gz
python3 dnac_map_migrate.py --import-only OUT.tar.gz         # preview + yes gate
```

### Configuration (`.env`)

The default flow and the `--export-only` / `--import-only` modes read settings
from a `.env` file next to the script, so they can run with fewer prompts. Real environment
variables override the file.

```bash
cp .env.example .env
vi .env
```

| Variable | Used by | Purpose |
|---|---|---|
| `DNAC_HOST` | all modes | Old (source) Catalyst Center host/IP |
| `DNAC_NEW_HOST` | default flow, `--import-only` | New (destination) host/IP. Required; no longer defaults to the source |
| `DNAC_USER` | export, import | Username (same account used on both systems) |
| `DNAC_PASS` | export, import | Password |
| `DNAC_SITE` | default flow, `--export-only` | Exact hierarchy name to export without prompting, e.g. `Global/Australia/Western Australia/Perth/Cisco Perth Office`. Leave empty to pick from a list |
| `DNAC_DEST` | default flow | Destination path on the new system (default: same as source) |
| `DNAC_KEEP_APS` | default flow | Set `true` to keep AP placements in the archive (default: removed) |
| `DNAC_OUT` | `--export-only` | Directory for exported archives (default `map_export_out`) |

If a value is missing the script prompts for it. `.env` and `map_export_out/`
are gitignored: never commit credentials or customer map archives (this is a
public repository). The final `yes` before an import is always asked.

### 500-map export limit

The Export Map Archive API caps a single export at 500 maps (floors) per
hierarchy element. If you pick an area large enough to exceed that, the
export call will fail — export smaller sub-areas instead.

### Name-matching caveats

The pre-export cross-check flags:

- **MISSING** — a building or floor with that name doesn't exist anywhere
  in the new system's hierarchy.
- **AMBIGUOUS** — a building name matches more than one site on the new
  system (not just the one under the intended parent), which could cause
  maps to attach to the wrong location.

This cross-check is a name-based heuristic to catch obvious problems
(typos, case differences, duplicates) *before* spending time on an
export/upload — it is not a guarantee of how the import will actually
behave. Treat the Import Status preview shown just before the final
confirmation as the authoritative check.

## Safety

- The script never overwrites anything without an explicit `y`/`yes`
  confirmation at the import step.
- SSL certificate verification is disabled by default (typical for
  internal Catalyst Center appliances using self-signed certs).
- Downloaded export archives are written to a temporary directory and
  deleted after each site is processed.
