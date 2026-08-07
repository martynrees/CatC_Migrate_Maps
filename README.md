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

## Important limitations and assumptions

This tool was built against Catalyst Center's Map Archive
export/import APIs, which are only partially documented publicly. A few
things are **inferred, not confirmed**, and are marked as such directly in
the code:

- **Destination matching is by name, not by ID or path.** Neither the
  Export Map Archive nor Import Map Archive APIs expose any
  `siteId`/`domain`/hierarchy parameter, and the exported archive itself
  does not contain the source system's full hierarchy path as text. The
  working theory — consistent with everything observed — is that
  Catalyst Center matches imported buildings/floors to **existing**
  buildings/floors on the destination system by name. This tool assumes
  the target buildings and floors already exist on the new system before
  you run it (create them via the normal Sites workflow first).
- **The upload field name for Start Import** (`file`) is inferred from
  the convention used by every other binary-upload endpoint in this API
  family (software image import, generic file upload), not confirmed
  against this specific endpoint's request schema.
- **The file reference produced by the export task** (used to download
  the resulting tar.gz) is located with a best-effort recursive search for
  a `fileId`-shaped key in the completed task response, since its exact
  schema isn't documented either. If it can't be found automatically, the
  script prints the raw task JSON and lets you paste the file ID in
  manually.

If either of the two inferred points above needs adjusting for your
Catalyst Center release, the raw server response is always printed on
failure — check `start_import()` and `export_site_maps()` in
`dnac_map_migrate.py`.

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
