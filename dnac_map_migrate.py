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

def load_dotenv(path=None):
    """Minimal .env loader (KEY=VALUE per line, # comments). Real environment
    variables win over the file. Keeps credentials out of the repo and out of
    prompts so the tool can run unattended."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


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
    # additionalInfo order is not guaranteed; scan every entry for the type.
    for info in site.get("additionalInfo") or []:
        stype = (info.get("attributes") or {}).get("type")
        if stype:
            return stype
    return ""


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


# Verified against a lab Catalyst Center: this endpoint answers 415 to every
# application/json request (and to bare/empty POSTs), but accepts
# Content-Type: text/plain and returns 202 + taskId. The empty-body case
# returns 400 NCMP00195. The body is used as the archive's file name
# (a body of "{}" fails the task with NCFS10055 "{}.tar.gz contains special
# characters"), so send a plain alphanumeric name.
def _export_variants(name):
    return [
        ("CT text/plain, body = file name",
         {"Content-Type": "text/plain", "Accept": "application/json"}, name),
    ]


def _save_archive(content, download_dir, site_uuid):
    filename = f"map_export_{site_uuid}.tar.gz"
    path = os.path.join(download_dir, filename)
    with open(path, "wb") as fh:
        fh.write(content)
    print(f"Saved export to {path} ({os.path.getsize(path)} bytes)")
    return path


def _download_by_context(base_url, api, context_id, download_dir, site_uuid):
    # INFERRED: some releases expose GET /maps/export/{id}/download.
    url = f"{base_url}/dna/intent/api/v1/maps/export/{context_id}/download"
    r = requests.get(url, headers=maps_api_headers(api), verify=False, timeout=300)
    if r.ok and r.content:
        return _save_archive(r.content, download_dir, site_uuid)
    print(f"  download via {url} -> HTTP {r.status_code}: {r.text[:200]}")
    return None


def export_site_maps(api, base_url, site_uuid, download_dir):
    print(f"\nRequesting map export for site UUID {site_uuid} ...")
    url = f"{base_url}/dna/intent/api/v1/maps/export/{site_uuid}"
    resp = None
    for label, extra, body in _export_variants(f"mapexport{int(time.time())}"):
        headers = {**maps_api_headers(api), **extra}
        r = requests.post(url, headers=headers, data=body, verify=False, timeout=300)
        print(f"  [{label}] -> HTTP {r.status_code} "
              f"ct={r.headers.get('Content-Type')!r} len={len(r.content)}")
        if r.ok:
            resp = r
            break
        print(f"     {r.text[:300]}")
    if resp is None:
        raise RuntimeError(
            "Every export request variant was rejected (see lines above). "
            "Run probe_export2.py to check the maps service and site type."
        )

    # Documented: 200/202 with response.taskId + response.url; poll the Task API.
    try:
        payload = resp.json()
    except ValueError:
        raise RuntimeError(f"Unrecognised export response: {resp.text[:500]}")
    inner = payload.get("response", payload) if isinstance(payload, dict) else payload
    raw_task_id = inner.get("taskId") if isinstance(inner, dict) else None
    task_id = raw_task_id.get("id") if isinstance(raw_task_id, dict) else raw_task_id
    if not task_id:
        print("Unexpected export response, no taskId:")
        print(json.dumps(payload, indent=2, default=str))
        raise RuntimeError("Cannot continue without a taskId.")

    task = wait_for_task(api, task_id, "map export")

    # INFERRED: the task result references a fileId (file API) or an export
    # context id (maps download route). Try both before asking the operator.
    # Verified: the finished task carries data="/file/<fileId>".
    file_id = None
    data_ref = task.get("data")
    if isinstance(data_ref, str) and data_ref.startswith("/file/"):
        file_id = data_ref.rsplit("/", 1)[-1]
    file_id = file_id or find_key_recursive(task, {"fileid"})
    if file_id:
        print(f"Downloading exported archive (file id {file_id}) ...")
        filename = f"map_export_{site_uuid}.tar.gz"
        api.file.download_a_file_by_file_id(
            file_id=file_id, dirpath=download_dir, save_file=True, filename=filename
        )
        path = os.path.join(download_dir, filename)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            print(f"Saved export to {path} ({os.path.getsize(path)} bytes)")
            return path

    ctx = find_key_recursive(task, {"exportcontextuuid", "contextid", "exportid"})
    if ctx:
        path = _download_by_context(base_url, api, ctx, download_dir, site_uuid)
        if path:
            return path

    print("\nCould not locate the exported archive automatically. Task response:")
    print(json.dumps(task, indent=2, default=str))
    raise RuntimeError("Export task finished but no downloadable file reference found.")


def summarise_archive(path):
    """Show what is inside the archive so the export can be proven (floor
    plan images, XML) without importing anywhere."""
    import tarfile

    try:
        with tarfile.open(path) as tf:
            names = tf.getnames()
    except tarfile.TarError as exc:
        print(f"Archive is not a readable tar: {exc}")
        return
    images = [n for n in names if n.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".svg"))]
    xmls = [n for n in names if n.lower().endswith(".xml")]
    print(f"Archive contents: {len(names)} entries, {len(xmls)} xml, {len(images)} images")
    for n in names[:40]:
        print(f"   {n}")


def maps_api_headers(api):
    return {"X-Auth-Token": api.access_token}


def start_import(base_url, headers, archive_path):
    url = f"{base_url}/dna/intent/api/v1/maps/import/start"
    print(f"\nUploading {archive_path} to {url} ...")
    # Verified on a lab Catalyst Center: the multipart field name is "archive"
    # ("file", "upload", "fileUpload", "importFile" all return 400; raw bodies 415).
    with open(archive_path, "rb") as fh:
        files = {"archive": (os.path.basename(archive_path), fh, "application/gzip")}
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


MAPS_XML = "xmlDir/MapsImportExport.xml"
_NS = "http://importexport.cisco.com/1.0"
_HIER_TAGS = {f"{{{_NS}}}Site", f"{{{_NS}}}Building", f"{{{_NS}}}Floor"}


def _children_named(node, name):
    return [c for c in node if c.tag in _HIER_TAGS and c.get("name") == name]


def _find_path(root, path):
    node = root.find(f"{{{_NS}}}Maps")
    for name in path:
        found = _children_named(node, name)
        if not found:
            raise RuntimeError(f"Path element '{name}' not found in archive XML")
        node = found[0]
    return node


def remap_archive(src, dst, old_path, new_path):
    """Rewrite the hierarchy nesting in an exported archive so import lands
    the subtree at old_path under new_path instead. Paths are lists of names
    WITHOUT the leading 'Global' (as stored in the XML). Missing intermediate
    areas on the new path are added as Site elements; the last name of
    new_path renames the moved node."""
    import copy
    import io
    import tarfile
    import xml.etree.ElementTree as ET

    ET.register_namespace("", _NS)
    ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")

    with tarfile.open(src) as tf:
        members = [(m, tf.extractfile(m).read() if m.isfile() else None) for m in tf.getmembers()]
    xml_bytes = next(d for m, d in members if m.name == MAPS_XML)
    root = ET.fromstring(xml_bytes)

    moved = _find_path(root, old_path)
    moved_copy = copy.deepcopy(moved)
    moved_copy.set("name", new_path[-1])

    # remove old chain (prune ancestors left without hierarchy children)
    chain = [root.find(f"{{{_NS}}}Maps")]
    for name in old_path:
        chain.append(_children_named(chain[-1], name)[0])
    for parent, child in reversed(list(zip(chain, chain[1:]))):
        parent.remove(child)
        if any(c.tag in _HIER_TAGS for c in parent):
            break

    # build/reuse new ancestors, then attach
    node = root.find(f"{{{_NS}}}Maps")
    for i, name in enumerate(new_path[:-1]):
        found = _children_named(node, name)
        if found:
            node = found[0]
        else:
            # a floor's direct parent must be a Building, anything else an area
            is_last = i == len(new_path) - 2
            tag = "Building" if (is_last and moved.tag.endswith("Floor")) else "Site"
            node = ET.SubElement(node, f"{{{_NS}}}{tag}", {"name": name})
    node.append(moved_copy)

    out_xml = ET.tostring(root, encoding="UTF-8", xml_declaration=True)
    with tarfile.open(dst, "w:gz") as tf:
        for member, data in members:
            if member.name == MAPS_XML:
                member.size = len(out_xml)
                tf.addfile(member, io.BytesIO(out_xml))
            elif data is not None:
                tf.addfile(member, io.BytesIO(data))
            else:
                tf.addfile(member)
    return dst


def _split(path):
    parts = [p for p in path.strip("/").split("/") if p]
    if parts and parts[0] == "Global":
        parts = parts[1:]
    return parts


def import_only():
    """python3 dnac_map_migrate.py --import-only ARCHIVE  (preview + yes gate)."""
    load_dotenv()
    archive = sys.argv[sys.argv.index("--import-only") + 1]
    host = os.environ.get("DNAC_NEW_HOST") or os.environ.get("DNAC_HOST") or input("Target host/IP: ").strip()
    user = os.environ.get("DNAC_USER") or input("Username: ").strip()
    pw = os.environ.get("DNAC_PASS") or getpass.getpass("Password: ")
    api = connect(host, user, pw, "target system")
    import_site_maps(api, f"https://{host}", archive)


def export_only():
    load_dotenv()
    """Lab/proof mode: export from one system, keep the archive locally and
    list its contents. Credentials may come from DNAC_HOST/DNAC_USER/DNAC_PASS."""
    host = os.environ.get("DNAC_HOST") or input("Source host/IP: ").strip()
    user = os.environ.get("DNAC_USER") or input("Username: ").strip()
    pw = os.environ.get("DNAC_PASS") or getpass.getpass("Password: ")
    api = connect(host, user, pw, "source system")
    sites = list_sites(api)
    if not sites:
        sys.exit("No sites found.")
    out_dir = os.path.abspath(os.environ.get("DNAC_OUT", "map_export_out"))
    os.makedirs(out_dir, exist_ok=True)
    wanted = os.environ.get("DNAC_SITE", "").strip()
    while True:
        if wanted:
            matches = [x for x in sites if site_hierarchy_name(x) == wanted]
            if not matches:
                sys.exit(f"DNAC_SITE '{wanted}' not found on {host}.")
            chosen = matches[0]
        else:
            chosen = choose_site(sites, "Select the site to export:")
        try:
            path = export_site_maps(api, f"https://{host}", chosen.get("id"), out_dir)
            summarise_archive(path)
        except (RuntimeError, TimeoutError, ApiError, requests.RequestException) as exc:
            print(f"\nERROR: {exc}")
        if wanted or not yes_no("\nExport another site?", default_no=True):
            break


def main():
    load_dotenv()
    if "--remap" in sys.argv:
        i = sys.argv.index("--remap")
        src, old, new, dst = sys.argv[i + 1 : i + 5]
        remap_archive(src, dst, _split(old), _split(new))
        print(f"Wrote {dst}: {old} -> {new}")
        return
    if "--import-only" in sys.argv:
        return import_only()
    if "--export-only" in sys.argv:
        return export_only()
    print("Catalyst Center map migration tool")
    print("===================================")

    old_host = input("Old system host/IP (source, e.g. dnac-old.example.com): ").strip()
    new_host = input("New system host/IP (destination): ").strip()
    username = input("Username (used for both systems): ").strip()
    password = getpass.getpass("Password (used for both systems): ")

    old_api = connect(old_host, username, password, "old system")
    new_api = connect(new_host, username, password, "new system")
    old_base_url = f"https://{old_host}"
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
                archive_path = export_site_maps(old_api, old_base_url, site_uuid, download_dir)
                summarise_archive(archive_path)
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
