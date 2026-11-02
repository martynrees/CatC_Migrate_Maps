# CatC_Migrate_Maps

Interactive Python tool to migrate Catalyst Center site maps (buildings,
floors, AP placement, RF calibration) from one Catalyst Center system to
another, one site at a time.

Built for repeated use as new areas are rolled out — run it again for
each area, it is not a one-off migration script.

## What it does

1. Prompts for the old (source) system host, the new (destination) system
   host, and one username/password used against both.
2. Lists the site hierarchy on the **old** system and lets you pick which
   site (area, building, or floor) to export maps from. Picking an area
   exports every building and floor nested underneath it in one archive.
3. Fetches the site hierarchy on the **new** system and cross-checks every
   building (and its floors) under the chosen scope by name, reporting
   `[OK]` / `[MISSING]` / `[AMBIGUOUS]` before doing anything else.
4. Exports the map archive from the old system and downloads it locally.
5. Uploads it to the new system and starts an import.
6. Prints the pre-import validation/preview and requires an explicit `yes`
   before committing — a Map import fully replaces existing map data for
   any matched site, so this is a deliberate gate, not a formality.
7. Loops, asking whether to migrate another site.

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
  target is reported "doesn't exist ... will be ignored". Create the target
  hierarchy (exact names) first.
- **Remap:** `--remap` rewrites the XML nesting so an exported subtree lands
  on a different existing path. Preview confirmed on an existing floor;
  a committed import has not yet been tested.

```bash
python3 dnac_map_migrate.py --export-only                    # uses .env
python3 dnac_map_migrate.py --remap IN.tar.gz "Global/old/path" "Global/new/path" OUT.tar.gz
python3 dnac_map_migrate.py --import-only OUT.tar.gz         # preview + yes gate
```

### Configuration (`.env`)

The `--export-only` and `--import-only` modes read settings from a `.env`
file next to the script, so they can run without prompts. Real environment
variables override the file.

```bash
cp .env.example .env
vi .env
```

| Variable | Used by | Purpose |
|---|---|---|
| `DNAC_HOST` | export, import | Catalyst Center host/IP (source for export; also the import target unless `DNAC_NEW_HOST` is set) |
| `DNAC_NEW_HOST` | `--import-only` | Optional separate destination host/IP |
| `DNAC_USER` | export, import | Username (same account used on both systems) |
| `DNAC_PASS` | export, import | Password |
| `DNAC_SITE` | `--export-only` | Exact hierarchy name to export without prompting, e.g. `Global/Australia/Western Australia/Perth/Cisco Perth Office`. Leave empty to pick from a list |
| `DNAC_OUT` | `--export-only` | Directory for exported archives (default `map_export_out`) |

If a value is missing the script prompts for it. `.env` and `map_export_out/`
are gitignored: never commit credentials or customer map archives (this is a
public repository). The interactive default mode (`python3 dnac_map_migrate.py`)
does not read `.env` and still prompts for everything.

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
