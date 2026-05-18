"""Reverse-engineer how AdMob Network waterfall lines are actually built.

ADFLUX (a working tool) creates AdMob Network waterfall lines via the API.
We earlier hit FAILED_PRECONDITION. This script pins down the exact
mechanism by analysing a KNOWN-WORKING group in this account:
"face yoga native" (group 7275396540).

It answers: are the per-line "backing ad units" regular ad units that
adUnits.list returns? If yes -> we can create them too. If no -> they're
provisioned a different way and we inspect how.

Run:  python investigate_waterfall.py
"""
from __future__ import annotations
import json, os, sqlite3, sys
from pathlib import Path

os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

DB_PATH = Path(__file__).resolve().parent / "admob_tool.db"
SAMPLE_GROUP_ID = "7275396540"  # "face yoga native" — known-working


def load():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as GReq
    from pydantic_settings import BaseSettings, SettingsConfigDict

    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT access_token, refresh_token, token_uri, scopes, "
        "(SELECT admob_publisher_id FROM users WHERE id=oauth_tokens.user_id) "
        "FROM oauth_tokens LIMIT 1"
    ).fetchone()
    conn.close()
    access, refresh, uri, scopes, pub = row

    class S(BaseSettings):
        model_config = SettingsConfigDict(env_file=".env", extra="ignore")
        google_client_id: str = ""
        google_client_secret: str = ""
    s = S()
    creds = Credentials(
        token=access, refresh_token=refresh or None,
        token_uri=uri or "https://oauth2.googleapis.com/token",
        client_id=s.google_client_id, client_secret=s.google_client_secret,
        scopes=(scopes or "").split(",") if scopes else None,
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(GReq())
    return creds, pub


def main():
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    creds, pub = load()
    svc = build("admob", "v1beta", credentials=creds, cache_discovery=False)
    print(f"Publisher: {pub}\n")

    # 1. Full ad unit list (paginated) -> set of all ad unit ids
    print("=== Step 1: full adUnits.list (all pages) ===")
    all_ids: set[str] = set()
    page = None
    pages = 0
    while True:
        kw = {"parent": f"accounts/{pub}", "pageSize": 1000}
        if page:
            kw["pageToken"] = page
        r = svc.accounts().adUnits().list(**kw).execute()
        for u in r.get("adUnits", []) or []:
            all_ids.add(str(u.get("adUnitId", "")))
        pages += 1
        page = r.get("nextPageToken")
        if not page:
            break
    print(f"  total ad units across {pages} page(s): {len(all_ids)}\n")

    # 2. Dump the known-working group, collect its waterfall-line mappings
    print(f"=== Step 2: group {SAMPLE_GROUP_ID} waterfall lines ===")
    grp = None
    page = None
    while True:
        kw = {"parent": f"accounts/{pub}", "pageSize": 200}
        if page:
            kw["pageToken"] = page
        r = svc.accounts().mediationGroups().list(**kw).execute()
        for g in r.get("mediationGroups", []) or []:
            if str(g.get("mediationGroupId", "")) == SAMPLE_GROUP_ID:
                grp = g
                break
        if grp:
            break
        page = r.get("nextPageToken")
        if not page:
            break
    if not grp:
        print(f"  group {SAMPLE_GROUP_ID} not found"); sys.exit(1)

    targeted = (grp.get("targeting", {}) or {}).get("adUnitIds", []) or []
    print(f"  group targets {len(targeted)} ad unit(s): {targeted}")

    # collect (line_name, adSourceId, mapping_resource_name) for MANUAL lines
    line_mappings = []
    for key, line in (grp.get("mediationGroupLines", {}) or {}).items():
        if (line.get("cpmMode") or "").upper() != "MANUAL":
            continue
        for au_id, map_name in (line.get("adUnitMappings", {}) or {}).items():
            line_mappings.append({
                "line": line.get("displayName", ""),
                "adSourceId": line.get("adSourceId", ""),
                "on_ad_unit": au_id,
                "mapping_resource": map_name,
            })
    print(f"  found {len(line_mappings)} MANUAL-line mapping reference(s)\n")

    # 3. For each targeted ad unit, list its adUnitMappings, get config values
    print("=== Step 3: inspect AdUnitMappings on targeted ad units ===")
    backing_ids: set[str] = set()
    for au in targeted:
        short = au.split("/")[-1]
        try:
            r = svc.accounts().adUnits().adUnitMappings().list(
                parent=f"accounts/{pub}/adUnits/{short}",
            ).execute()
        except HttpError as e:
            print(f"  {au}: list failed {e}")
            continue
        maps = r.get("adUnitMappings", []) or []
        print(f"  ad unit {short}: {len(maps)} mapping(s)")
        for m in maps:
            cfg = m.get("adUnitConfigurations", {}) or {}
            for cfg_id, cfg_val in cfg.items():
                # config values that look like an ad unit id
                if "ca-app-pub" in str(cfg_val):
                    backing_ids.add(str(cfg_val))
                    in_list = str(cfg_val) in all_ids
                    print(f"     adapter={m.get('adapterId','?')} "
                          f"cfg[{cfg_id}]={cfg_val}  "
                          f"-> in adUnits.list? {'YES' if in_list else 'NO'}")
    print()

    # 4. Verdict
    print("=== VERDICT ===")
    if not backing_ids:
        print("No ad-unit-id config values found in the mappings.")
    else:
        in_cnt = sum(1 for b in backing_ids if b in all_ids)
        out_cnt = len(backing_ids) - in_cnt
        print(f"backing ad units referenced: {len(backing_ids)}")
        print(f"  present in adUnits.list:     {in_cnt}")
        print(f"  NOT in adUnits.list:         {out_cnt}")
        print()
        if in_cnt == len(backing_ids):
            print(">>> Backing ad units ARE regular ad units. We CAN create")
            print(">>> them via adUnits.create. The earlier FAILED_PRECONDITION")
            print(">>> had another cause — likely format/sequence. Fixable.")
        elif out_cnt == len(backing_ids):
            print(">>> Backing ad units are NOT in the regular list. They are")
            print(">>> provisioned a different way — share this output, we")
            print(">>> dig into how AdMob/ADFLUX makes them.")
        else:
            print(">>> Mixed — share this output for analysis.")


if __name__ == "__main__":
    main()
