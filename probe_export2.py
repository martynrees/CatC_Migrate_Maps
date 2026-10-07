"""Diagnostic 2: is the maps service reachable on this system at all, and
does the export route exist for other site types?

Read-only except that a successful export POST creates an export task.
"""
import getpass

import requests
import urllib3
from dnacentersdk import DNACenterAPI

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

host = input("Old system host/IP: ").strip()
user = input("Username: ").strip()
pw = getpass.getpass("Password: ")
api = DNACenterAPI(username=user, password=pw, base_url=f"https://{host}", verify=False)
H = {"X-Auth-Token": api.access_token}
B = f"https://{host}/dna/intent/api/v1"


def show(label, r):
    print(f"{label:52} -> {r.status_code} ct={r.headers.get('Content-Type')!r} len={len(r.content)}")
    if r.content and "json" in (r.headers.get("Content-Type") or ""):
        print("    ", r.text[:300].replace("\n", " "))


# 1. Control: other maps-service endpoints.
for path in ("/maps/supported-access-points",):
    show(f"GET {path}", requests.get(B + path, headers=H, verify=False, timeout=60))

# 2. Sites to try: area, building, floor.
want = {
    "area St Albans": "Global/Australia/VIC/St Albans",
    "building ST8": "Global/Australia/VIC/St Albans/ST8",
    "floor ST8/Level 1": "Global/Australia/VIC/St Albans/ST8/Level 1",
}
uuids = {}
for label, name in want.items():
    try:
        res = api.sites.get_site(name=name)
        res = res.response if hasattr(res, "response") else res
        uuids[label] = res[0]["id"]
    except Exception as exc:
        print(f"could not resolve {label}: {exc}")

# 3. Does the export route exist (method probing) and accept other UUIDs?
for label, u in uuids.items():
    url = f"{B}/maps/export/{u}"
    print(f"\n{label}  {u}")
    show("  OPTIONS", requests.options(url, headers=H, verify=False, timeout=60))
    show("  GET", requests.get(url, headers=H, verify=False, timeout=60))
    show("  POST (no body)", requests.post(url, headers=H, verify=False, timeout=120))
    show("  POST json {}", requests.post(url, headers={**H, "Content-Type": "application/json"}, data="{}", verify=False, timeout=120))

# 4. Control: a made-up path under the same prefix.
show("\nPOST /maps/export-does-not-exist/x", requests.post(f"{B}/maps/export-does-not-exist/x", headers=H, verify=False, timeout=60))
