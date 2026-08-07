#!/usr/bin/env python3
"""
dnac_map_migrate.py

Interactive tool to migrate Catalyst Center site maps (buildings/floors,
AP placement, RF calibration) from one Catalyst Center system to another,
one site at a time. Built for repeated use as new areas are rolled out --
run it again for each area, it is not a one-off script.

Flow:
  1. Prompt for old-system host, new-system host, and one username/password
     used against both.
  2. List the site hierarchy on the OLD system, let the operator pick which
     site (area/building/floor) to export maps from.
  3. Export the map archive from the old system and download it locally.
  4. Upload it to the NEW system and start an import.
  5. Print the pre-import validation/preview and require an explicit "yes"
     before committing -- a Map import fully replaces existing map data for
     any matched site, so this is a deliberate safety gate, not a formality.
  6. Loop, asking whether to do another site.

Requirements:
    pip install dnacentersdk

Two steps in this script rely on Catalyst Center's Map Archive
import/export APIs, which are only partially documented publicly (no
publicly documented multipart field name for the archive upload, and no
publicly documented shape for the file reference produced by the export
task). Both spots are marked "INFERRED" below. If either fails on first
run, the script prints the raw server response so the failure is visible
and fixable rather than silent -- if the field name or JSON shape differs
on your Catalyst Center release, adjust the marked spot and it should keep
working for every subsequent site you migrate.
"""

import getpass
import json
import os
import sys
import tempfile
import time

import requests
import urllib3
from dnacentersdk import DNACenterAPI
from dnacentersdk.exceptions import ApiError

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

POLL_INTERVAL_SECONDS = 5
TASK_TIMEOUT_SECONDS = 900


def connect(host, username, password, label):
    print(f"\nConnecting to {label} ({host}) ...")
    try:
        api = DNACenterAPI(
            username=username,
            password=password,
            base_url=f"https://{host}",
            verify=False,
        )
    except ApiError as exc:
        sys.exit(f"Could not authenticate to {label} ({host}): {exc}")
    print(f"Connected to {label}.")
    return api


def list_sites(api):
    sites = []
    offset = 1
    limit = 500
    while True:
        resp = api.sites.get_site(offset=offset, limit=limit)
        page = resp.response if hasattr(resp, "response") else resp
        if not page:
            break
        sites.extend(page)
        if len(page) < limit:
            break
        offset += limit
    return sites


def site_hierarchy_name(site):
    return (
        site.get("siteNameHierarchy")
        or site.get("nameHierarchy")
        or site.get("name")
        or site.get("id")
    )


def site_type(site):
    if not site.get("additionalInfo"):
        return ""
    return site["additionalInfo"][0].get("attributes", {}).get("type", "")


def site_leaf_name(site):
    name = site.get("name")
    if name:
        return name
    hierarchy = site_hierarchy_name(site)
    return hierarchy.rsplit("/", 1)[-1] if hierarchy else hierarchy


def choose_site(sites, prompt):
    ordered = sorted(sites, key=site_hierarchy_name)
    print(f"\n{prompt}")
    for idx, site in enumerate(ordered, start=1):
        stype = site_type(site)
        suffix = f"  [{stype}]" if stype else ""
        print(f"  [{idx}] {site_hierarchy_name(site)}{suffix}")
    while True:
        choice = input("Enter number: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(ordered):
            return ordered[int(choice) - 1]
        print("Invalid selection, try again.")


def _norm(name):
    return (name or "").strip().lower()


def sites_in_scope(chosen, all_sites):
    """All sites at or below the chosen node, per the same 'all child
    elements starting at this hierarchy element' scoping the Export Map
    Archive API itself uses."""
    chosen_hierarchy = site_hierarchy_name(chosen)
    in_scope = []
    for site in all_sites:
        hierarchy = site_hierarchy_name(site)
        if hierarchy == chosen_hierarchy or hierarchy.startswith(chosen_hierarchy + "/"):
            in_scope.append(site)
    return in_scope


def compare_hierarchies(chosen, old_sites, new_sites):
    """Cross-check building and floor names under the chosen old-system
    scope against whatever exists anywhere in the new system's hierarchy.

    This assumes Catalyst Center's map import matches sites by name rather
    than by a shared ID or path (a working theory, not a documented fact --
    see the module docstring). Returns (report_lines, clean) where clean is
    False if anything didn't match cleanly, ambiguously matched more than
    once, or is missing.
    """
    old_scope = sites_in_scope(chosen, old_sites)
    old_buildings = [s for s in old_scope if site_type(s) == "building"]
    old_floors = [s for s in old_scope if site_type(s) == "floor"]

    new_buildings_by_name = {}
    for s in new_sites:
        if site_type(s) == "building":
            new_buildings_by_name.setdefault(_norm(site_leaf_name(s)), []).append(s)
    new_floors_by_parent = {}
    for s in new_sites:
        if site_type(s) == "floor":
            new_floors_by_parent.setdefault(s.get("parentId"), {})[
                _norm(site_leaf_name(s))
            ] = s

    report = []
    clean = True

    if not old_buildings:
        report.append(
            f"No buildings found under '{site_hierarchy_name(chosen)}' on the old "
            "system (selection may itself be a building or floor)."
        )

    for building in old_buildings:
        name = site_leaf_name(building)
        matches = new_buildings_by_name.get(_norm(name), [])
        floors_here = [
            f for f in old_floors if f.get("parentId") == building.get("id")
        ]

        if not matches:
            clean = False
            report.append(f"[MISSING]   Building '{name}' -- not found on new system")
            continue
        if len(matches) > 1:
            clean = False
            paths = ", ".join(site_hierarchy_name(m) for m in matches)
            report.append(
                f"[AMBIGUOUS] Building '{name}' -- matches {len(matches)} sites on "
                f"new system: {paths}"
            )
            continue

        new_building = matches[0]
        report.append(
            f"[OK]        Building '{name}' -- matched at "
            f"{site_hierarchy_name(new_building)}"
        )
        new_floor_names = new_floors_by_parent.get(new_building.get("id"), {})
        for floor in floors_here:
            floor_name = site_leaf_name(floor)
            if _norm(floor_name) in new_floor_names:
                report.append(f"            [OK]      Floor '{floor_name}'")
            else:
                clean = False
                report.append(
                    f"            [MISSING] Floor '{floor_name}' -- not found "
                    f"under matched building on new system"
                )

    return report, clean


def yes_no(prompt, default_no=True):
    suffix = "[y/N]" if default_no else "[Y/n]"
    answer = input(f"{prompt} {suffix}: ").strip().lower()
    if not answer:
        return not default_no
    return answer in ("y", "yes")


def find_key_recursive(obj, target_keys):
    """Best-effort search for a fileId-shaped value anywhere in a task
    response, since the exact schema DNAC uses for the export task's
    result reference is not documented publicly (INFERRED)."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.lower() in target_keys and isinstance(value, str) and value:
                return value
            found = find_key_recursive(value, target_keys)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_key_recursive(item, target_keys)
            if found:
                return found
    elif isinstance(obj, str):
        stripped = obj.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return find_key_recursive(json.loads(stripped), target_keys)
            except (ValueError, TypeError):
                return None
    return None


def wait_for_task(api, task_id, label):
    print(f"Waiting for {label} (task {task_id}) ...")
    deadline = time.time() + TASK_TIMEOUT_SECONDS
    while time.time() < deadline:
        task = api.task.get_task_by_id(task_id=task_id).response
        if task.get("isError"):
            raise RuntimeError(
                f"{label} failed: {task.get('failureReason') or task.get('progress') or task}"
            )
        if task.get("endTime"):
            print(f"{label} completed.")
            return task
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(f"{label} did not complete within {TASK_TIMEOUT_SECONDS}s")


def export_site_maps(api, site_uuid, download_dir):
    print(f"\nRequesting map export for site UUID {site_uuid} ...")
    resp = api.sites.export_map_archive(site_hierarchy_uuid=site_uuid).response
    raw_task_id = resp.get("taskId")
    task_id = raw_task_id.get("id") if isinstance(raw_task_id, dict) else raw_task_id
    if not task_id:
        raise RuntimeError(f"Unexpected export response, no taskId found: {resp}")

    task = wait_for_task(api, task_id, "map export")

    # INFERRED: the export task's completion payload should reference a
    # fileId that /dna/intent/api/v1/file/{fileId} can download. The exact
    # key name is not publicly documented, so we search for the common
    # variants used elsewhere in the Catalyst Center API.
    file_id = find_key_recursive(task, {"fileid"})
    if not file_id:
        print("\nCould not automatically find a fileId in the completed export task.")
        print("Full task response for manual inspection:")
        print(json.dumps(task, indent=2, default=str))
        file_id = input(
            "Enter the file ID to download manually (or leave blank to abort): "
        ).strip()
        if not file_id:
            raise RuntimeError("No file ID available, cannot download export archive.")

    print(f"Downloading exported archive (file id {file_id}) ...")
    filename = f"map_export_{site_uuid}.tar.gz"
    api.file.download_a_file_by_file_id(
        file_id=file_id, dirpath=download_dir, save_file=True, filename=filename
    )
    path = os.path.join(download_dir, filename)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise RuntimeError(f"Download reported success but {path} is missing/empty.")
    print(f"Saved export to {path} ({os.path.getsize(path)} bytes)")
    return path


def maps_api_headers(api):
    return {"X-Auth-Token": api.access_token}


def start_import(base_url, headers, archive_path):
    url = f"{base_url}/dna/intent/api/v1/maps/import/start"
    print(f"\nUploading {archive_path} to {url} ...")
    # INFERRED: multipart field name "file" follows the convention used by
    # every other binary-upload endpoint in this API (software image
    # import, generic file upload). Not confirmed against this specific
    # endpoint's (undocumented) request schema.
    with open(archive_path, "rb") as fh:
        files = {"file": (os.path.basename(archive_path), fh, "application/gzip")}
        resp = requests.post(url, headers=headers, files=files, verify=False, timeout=300)

    if resp.status_code not in (200, 202):
        print(f"Start Import failed: HTTP {resp.status_code}")
        print(resp.text)
        resp.raise_for_status()

    try:
        body = resp.json()
    except ValueError:
        body = resp.text.strip().strip('"')

    import_context_uuid = None
    if isinstance(body, str):
        import_context_uuid = body
    elif isinstance(body, dict):
        import_context_uuid = (
            body.get("importContextUuid")
            or body.get("response")
            or find_key_recursive(body, {"importcontextuuid"})
        )

    if not import_context_uuid:
        print("Could not determine importContextUuid from response:")
        print(resp.text)
        import_context_uuid = input("Enter importContextUuid manually: ").strip()

    print(f"Import context: {import_context_uuid}")
    return import_context_uuid


def get_import_status(base_url, headers, import_context_uuid):
    url = f"{base_url}/dna/intent/api/v1/maps/import/{import_context_uuid}/status"
    resp = requests.get(url, headers=headers, verify=False, timeout=120)
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        return resp.text


def perform_import(base_url, headers, import_context_uuid):
    url = f"{base_url}/dna/intent/api/v1/maps/import/{import_context_uuid}/perform"
    resp = requests.post(url, headers=headers, verify=False, timeout=300)
    if resp.status_code not in (200, 204):
        print(f"Perform Import failed: HTTP {resp.status_code}")
        print(resp.text)
        resp.raise_for_status()
    print("Import committed.")


def cancel_import(base_url, headers, import_context_uuid):
    url = f"{base_url}/dna/intent/api/v1/maps/import/{import_context_uuid}"
    resp = requests.delete(url, headers=headers, verify=False, timeout=60)
    if resp.status_code not in (200, 202, 204):
        print(f"Warning: cancel returned HTTP {resp.status_code}: {resp.text}")
    else:
        print("Import cancelled, no changes were made to the new system.")


def import_site_maps(new_api, new_base_url, archive_path):
    headers = maps_api_headers(new_api)
    import_context_uuid = start_import(new_base_url, headers, archive_path)

    print("Waiting for pre-import validation ...")
    time.sleep(POLL_INTERVAL_SECONDS)
    status = get_import_status(new_base_url, headers, import_context_uuid)

    print("\n--- Pre-import validation / preview ---")
    print(json.dumps(status, indent=2, default=str) if isinstance(status, dict) else status)
    print("---------------------------------------")

    proceed = yes_no(
        "\nThis will REPLACE existing map data for any matched site on the new "
        "system. Proceed with import?"
    )
    if not proceed:
        cancel_import(new_base_url, headers, import_context_uuid)
        return False

    perform_import(new_base_url, headers, import_context_uuid)
    return True


def main():
    print("Catalyst Center map migration tool")
    print("===================================")

    old_host = input("Old system host/IP (source, e.g. dnac-old.example.com): ").strip()
    new_host = input("New system host/IP (destination): ").strip()
    username = input("Username (used for both systems): ").strip()
    password = getpass.getpass("Password (used for both systems): ")

    old_api = connect(old_host, username, password, "old system")
    new_api = connect(new_host, username, password, "new system")
    new_base_url = f"https://{new_host}"

    print("\nFetching site hierarchy from the old system ...")
    old_sites = list_sites(old_api)
    if not old_sites:
        sys.exit("No sites found on the old system.")

    with tempfile.TemporaryDirectory(prefix="dnac_map_export_") as download_dir:
        while True:
            chosen = choose_site(
                old_sites, "Select the site to export maps from (old system):"
            )
            site_uuid = chosen.get("id")

            print("\nFetching site hierarchy from the new system for cross-check ...")
            new_sites = list_sites(new_api)
            report, clean = compare_hierarchies(chosen, old_sites, new_sites)

            print(f"\n--- Name cross-check: '{site_hierarchy_name(chosen)}' vs new system ---")
            for line in report:
                print(line)
            print("--------------------------------------------------------------")

            if not clean:
                print(
                    "\nSome buildings/floors did not cleanly match by name on the "
                    "new system (see MISSING/AMBIGUOUS lines above)."
                )
                if not yes_no("Proceed with export/import anyway?"):
                    print("Skipping this site.")
                    if not yes_no("\nMigrate another site?", default_no=False):
                        break
                    continue
            else:
                print("All buildings/floors matched by name. Proceeding.")

            try:
                archive_path = export_site_maps(old_api, site_uuid, download_dir)
                imported = import_site_maps(new_api, new_base_url, archive_path)
                if imported:
                    print(
                        f"\nDone: maps for '{site_hierarchy_name(chosen)}' migrated."
                    )
                else:
                    print(f"\nSkipped: import for '{site_hierarchy_name(chosen)}' was cancelled.")
            except (RuntimeError, TimeoutError, ApiError, requests.RequestException) as exc:
                print(f"\nERROR: {exc}")
                print("You can retry this site or move on to the next one.")
            finally:
                if os.path.exists(archive_path) if 'archive_path' in locals() else False:
                    os.remove(archive_path)

            if not yes_no("\nMigrate another site?", default_no=False):
                break

    print("\nAll done.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nAborted.")
