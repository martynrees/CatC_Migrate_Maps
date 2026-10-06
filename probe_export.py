"""Diagnostic: find which request shape the maps export endpoint accepts.

Read-only against Catalyst Center (an export only creates an archive; it
changes no site or map data). Tries several Content-Type / Accept / body
combinations against POST /dna/intent/api/v1/maps/export/{uuid} and prints
one line per attempt. Stops at the first 2xx and saves any binary response.
"""
import getpass
import sys

import requests
import urllib3
from dnacentersdk import DNACenterAPI

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

host = input("Old system host/IP: ").strip()
user = input("Username: ").strip()
pw = getpass.getpass("Password: ")
site_uuid = input("Site UUID to export (e.g. the St Albans UUID): ").strip()

api = DNACenterAPI(username=user, password=pw, base_url=f"https://{host}", verify=False)
token = api.access_token
url = f"https://{host}/dna/intent/api/v1/maps/export/{site_uuid}"

ANY = {}
variants = [
    ("no CT, Accept */*", {}, None),
    ("no CT, Accept json", {"Accept": "application/json"}, None),
    ("CT json, body {}", {"Content-Type": "application/json", "Accept": "*/*"}, "{}"),
    ("CT json, no body", {"Content-Type": "application/json", "Accept": "*/*"}, None),
    ("CT octet-stream", {"Content-Type": "application/octet-stream", "Accept": "*/*"}, None),
    ("CT x-www-form-urlencoded", {"Content-Type": "application/x-www-form-urlencoded", "Accept": "*/*"}, None),
    ("CT json, Accept octet-stream", {"Content-Type": "application/json", "Accept": "application/octet-stream"}, "{}"),
    ("Accept gzip/tar", {"Accept": "application/gzip, application/x-gzip, application/x-tar, */*"}, None),
]

for label, extra, body in variants:
    headers = {"X-Auth-Token": token, **extra}
    try:
        r = requests.post(url, headers=headers, data=body, verify=False, timeout=120)
    except requests.RequestException as exc:
        print(f"{label:34} -> ERROR {exc}")
        continue
    ctype = r.headers.get("Content-Type", "")
    print(f"{label:34} -> {r.status_code} ct={ctype!r} len={len(r.content)}")
    if r.ok:
        if "json" in ctype:
            print("   body:", r.text[:600])
        else:
            out = "probe_export_result.bin"
            with open(out, "wb") as fh:
                fh.write(r.content)
            print(f"   binary response saved to {out}")
        sys.exit(0)
    print("   body:", r.text[:300].replace("\n", " "))

print("\nNo variant succeeded.")
