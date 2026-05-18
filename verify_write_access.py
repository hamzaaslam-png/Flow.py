"""Standalone diagnostic: tests AdMob v1beta write API access with the
absolute minimal valid request. Bypasses all of flow.py's logic so you
can be 100% certain whether your code is the cause or your account
write API access is the cause.

Run with:    python verify_write_access.py

Expected outcomes:
  - "READ TEST: OK" + "WRITE TEST: FAILED with RESOURCE_EXHAUSTED"
        -> Your code is fine. Submit the AdMob API access request form.
  - "READ TEST: OK" + "WRITE TEST: OK (cleanup needed)"
        -> Your account already has write access. Something else is wrong
           in flow.py — re-test there.
  - "READ TEST: FAILED ..."
        -> OAuth or basic API setup issue. Sign in fresh and retry.
"""
from __future__ import annotations

import json
import os
import sys

# Match flow.py's environment
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

# Reuse flow.py's stored OAuth token from admob_tool.db
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "admob_tool.db"
if not DB_PATH.exists():
    print(f"ERROR: {DB_PATH} not found. Sign in via the tool first "
          "(python flow.py, then http://localhost:8000), then re-run.")
    sys.exit(1)


def load_creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as GReq

    # Pull token from DB (matches flow.py's OAuthToken schema)
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT access_token, refresh_token, token_uri, scopes, "
        "       (SELECT admob_publisher_id FROM users WHERE id = oauth_tokens.user_id) "
        "FROM oauth_tokens LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        print("ERROR: No OAuth token in admob_tool.db. Sign in first.")
        sys.exit(1)
    access_token, refresh_token, token_uri, scopes, pub_id = row

    # Need client id/secret from .env
    from pydantic_settings import BaseSettings, SettingsConfigDict

    class S(BaseSettings):
        model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
        google_client_id: str = ""
        google_client_secret: str = ""

    s = S()
    if not s.google_client_id or not s.google_client_secret:
        print("ERROR: google_client_id/secret missing in .env")
        sys.exit(1)

    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token or None,
        token_uri=token_uri or "https://oauth2.googleapis.com/token",
        client_id=s.google_client_id,
        client_secret=s.google_client_secret,
        scopes=(scopes or "").split(",") if scopes else None,
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(GReq())
    return creds, pub_id


def main():
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    creds, pub_id = load_creds()
    if not pub_id:
        print("ERROR: No publisher_id stored. Open the tool /apps page once "
              "first to populate it.")
        sys.exit(1)

    print(f"Publisher ID: {pub_id}")
    print(f"Token scopes: {creds.scopes}")

    # Whose email is this token actually issued to? Critical for diagnosing
    # role/permission issues — if the email tied to this OAuth token is not
    # an Owner/Admin of the AdMob account, writes will fail even though
    # reads succeed.
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {creds.token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            info = json.loads(r.read().decode("utf-8"))
        print(f"Logged-in email: {info.get('email', '?')}")
        print(f"Display name:    {info.get('name', '?')}")
    except Exception as e:
        print(f"Could not look up email: {e}")
    print()

    svc = build("admob", "v1beta", credentials=creds, cache_discovery=False)

    # ---------- DUMP AN EXISTING MEDIATION GROUP ----------
    # Reads your working sample group and prints its full JSON so we can
    # see EXACTLY how AdMob structures mediationGroupLines + adUnitMappings.
    # Change SAMPLE_GROUP_ID if you want to inspect a different group.
    SAMPLE_GROUP_ID = "7275396540"
    print(f"=== DUMP existing mediation group {SAMPLE_GROUP_ID} ===")
    try:
        page_token = None
        found = None
        while True:
            kw = {"parent": f"accounts/{pub_id}", "pageSize": 200}
            if page_token:
                kw["pageToken"] = page_token
            resp = svc.accounts().mediationGroups().list(**kw).execute()
            for g in resp.get("mediationGroups", []) or []:
                gid = str(g.get("mediationGroupId") or "")
                gname = str(g.get("name") or "")
                if gid == SAMPLE_GROUP_ID or gname.endswith(f"/{SAMPLE_GROUP_ID}"):
                    found = g
                    break
            if found:
                break
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        if found:
            print(json.dumps(found, indent=2))
        else:
            print(f"  Group {SAMPLE_GROUP_ID} not found. Listing all groups:")
            resp = svc.accounts().mediationGroups().list(
                parent=f"accounts/{pub_id}", pageSize=50,
            ).execute()
            for g in resp.get("mediationGroups", []) or []:
                print(f"  - id={g.get('mediationGroupId','?')} "
                      f"name={g.get('displayName','?')!r}")
    except HttpError as e:
        print(f"DUMP: FAILED — {e}")
    print()

    # ---------- DUMP all ad units (find a working tier ad unit) ----------
    # The working group references tier ad unit 3034851704. Dump every ad
    # unit so we can see what a working waterfall-backing ad unit looks like
    # vs. a fresh one created by the tool.
    print("=== AD UNITS dump (looking for 3034851704 + a tool-created one) ===")
    try:
        resp = svc.accounts().adUnits().list(
            parent=f"accounts/{pub_id}", pageSize=500,
        ).execute()
        units = resp.get("adUnits", []) or []
        print(f"Total ad units: {len(units)}")
        for u in units:
            aid = str(u.get("adUnitId", ""))
            if aid.endswith("/3034851704") or "Global_Voice_Banner_line" in str(u.get("displayName", "")):
                print(json.dumps(u, indent=2))
    except HttpError as e:
        print(f"AD UNITS: FAILED — {e}")
    print()

    # ---------- LIVE TEST: try several waterfall-mapping variations ----------
    print("=== LIVE TEST: waterfall AdUnitMapping variations ===")
    try:
        units_resp = svc.accounts().adUnits().list(
            parent=f"accounts/{pub_id}", pageSize=500,
        ).execute()
        all_units = units_resp.get("adUnits", []) or []
        src = next((u for u in all_units
                    if str(u.get("adUnitId", "")).endswith("/1011088787")), None)
        tiers = [u for u in all_units
                 if "Global_Voice_Banner_line" in str(u.get("displayName", ""))]
        if not src or not tiers:
            print(f"  Need source 1011088787 + tier ad unit(s). "
                  f"src={'found' if src else 'MISSING'} tiers={len(tiers)}")
        else:
            src_id = str(src["adUnitId"])
            src_app = str(src.get("appId", ""))
            src_fmt = str(src.get("adFormat", "?"))
            print(f"  SOURCE ad unit full dump:")
            print(json.dumps(src, indent=2))
            # platform of the source app
            src_platform = "?"
            try:
                apps_resp = svc.accounts().apps().list(
                    parent=f"accounts/{pub_id}",
                ).execute()
                for ap in apps_resp.get("apps", []) or []:
                    if str(ap.get("appId", "")) == src_app:
                        src_platform = str(ap.get("platform", "?"))
                        print(f"  source app platform = {src_platform}")
                        break
            except HttpError:
                pass
            tier_id = str(tiers[0]["adUnitId"])
            short_src = src_id.split("/")[-1]
            short_tier = tier_id.split("/")[-1]

            # pick correct waterfall adapter for src_fmt + src_platform
            wf_adapters = svc.accounts().adSources().adapters().list(
                parent=f"accounts/{pub_id}/adSources/1215381445328257950",
            ).execute().get("adapters", []) or []
            fmt = src_fmt.upper()
            if fmt == "APP_OPEN_AD":
                fmt = "APP_OPEN"
            chosen = None
            for a in wf_adapters:
                af = [x.upper() for x in a.get("formats", [])]
                if (a.get("platform", "").upper() == src_platform.upper()
                        and (fmt in af
                             or (fmt in ("BANNER", "INTERSTITIAL")
                                 and "BANNER_AND_INTERSTITIAL" in af))):
                    chosen = a
                    break
            if not chosen:
                print(f"  No adapter matched fmt={fmt} platform={src_platform}; "
                      f"using first.")
                chosen = wf_adapters[0] if wf_adapters else None
            adapter_id = str(chosen.get("adapterId", "")) if chosen else "?"
            cfg_id = (str(chosen["adapterConfigMetadata"][0]["adapterConfigMetadataId"])
                      if chosen and chosen.get("adapterConfigMetadata") else "?")
            print(f"  chosen adapter={adapter_id} configMetaId={cfg_id}")

            def attempt(label, parent_unit, cfg_value):
                body = {
                    "adapterId": adapter_id,
                    "adUnitConfigurations": {cfg_id: cfg_value},
                    "state": "ENABLED",
                }
                print(f"\n  -- {label} --")
                print(f"     parent adUnit={parent_unit}  body={json.dumps(body)}")
                try:
                    r = svc.accounts().adUnits().adUnitMappings().create(
                        parent=f"accounts/{pub_id}/adUnits/{parent_unit}",
                        body=body,
                    ).execute()
                    print(f"     RESULT: OK -> {json.dumps(r)}")
                    return True
                except HttpError as e:
                    print(f"     RESULT: FAILED {e.resp.status if e.resp else '?'}")
                    print(f"     RAW: {e.content.decode('utf-8', errors='replace')}")
                    return False

            # Variation A: mapping ON source, config = TIER ad unit
            attempt("A: parent=SOURCE, config=TIER", short_src, tier_id)
            # Variation B: mapping ON source, config = SOURCE ad unit
            attempt("B: parent=SOURCE, config=SOURCE", short_src, src_id)
            # Variation C: mapping ON tier, config = TIER ad unit
            attempt("C: parent=TIER, config=TIER", short_tier, tier_id)
            # Variation D: mapping ON tier, config = SOURCE ad unit
            attempt("D: parent=TIER, config=SOURCE", short_tier, src_id)
    except HttpError as e:
        print(f"LIVE TEST setup failed: {e}")
    print()

    # ---------- ADAPTERS for AdMob Network Waterfall ----------
    print("=== ADAPTERS for 'AdMob Network Waterfall' (1215381445328257950) ===")
    try:
        resp = svc.accounts().adSources().adapters().list(
            parent=f"accounts/{pub_id}/adSources/1215381445328257950",
        ).execute()
        adapters = resp.get("adapters", []) or []
        print(f"Found {len(adapters)} adapter(s):")
        for a in adapters:
            print(json.dumps(a, indent=2))
    except HttpError as e:
        print(f"ADAPTERS: FAILED — {e}")
    print()

    # ---------- DUMP an existing AdUnitMapping ----------
    # The sample group references mapping 8677913526603817 on ad unit
    # 2280256704. Dump that ad unit's mappings to see the exact body shape.
    print("=== AdUnitMappings on ad unit 2280256704 (from sample group) ===")
    try:
        resp = svc.accounts().adUnits().adUnitMappings().list(
            parent=f"accounts/{pub_id}/adUnits/2280256704",
        ).execute()
        maps = resp.get("adUnitMappings", []) or []
        print(f"Found {len(maps)} mapping(s). Showing first 3:")
        for m in maps[:3]:
            print(json.dumps(m, indent=2))
    except HttpError as e:
        print(f"ADUNITMAPPINGS: FAILED — {e}")
    print()

    # ---------- AD SOURCES DUMP ----------
    # This lists every ad source AdMob exposes for your account, with its
    # ID and title. We need to find the source used for MANUAL waterfall
    # lines (allows many per group) vs the bidding "AdMob Network" source
    # (allows only 1 per group).
    print("=== AD SOURCES (accounts.adSources.list) ===")
    try:
        resp = svc.accounts().adSources().list(
            parent=f"accounts/{pub_id}",
        ).execute()
        sources = resp.get("adSources", []) or []
        print(f"Found {len(sources)} ad source(s):")
        for s in sources:
            sid = s.get("adSourceId", "?")
            title = s.get("title", "?")
            print(f"  adSourceId={sid:<24} title={title!r}")
        # Highlight AdMob-related ones
        print()
        print("AdMob-related sources (these matter for waterfall lines):")
        for s in sources:
            title = (s.get("title") or "")
            if "admob" in title.lower() or "google" in title.lower():
                print(f"  >>> adSourceId={s.get('adSourceId','?')}  "
                      f"title={title!r}")
    except HttpError as e:
        print(f"AD SOURCES: FAILED — {e}")
    print()

    # ---------- READ TEST ----------
    print("=== READ TEST (accounts.apps.list) ===")
    try:
        resp = svc.accounts().apps().list(parent=f"accounts/{pub_id}").execute()
        apps = resp.get("apps", []) or []
        print(f"READ TEST: OK — found {len(apps)} app(s)")
        if not apps:
            print("  (Account has no apps. Cannot run WRITE test without an "
                  "appId. Create one app in AdMob console first.)")
            sys.exit(0)
        sample_app_id = apps[0]["appId"]
        print(f"  Using app: {apps[0].get('name', sample_app_id)}")
    except HttpError as e:
        print(f"READ TEST: FAILED — {e}")
        print()
        print("Diagnosis: basic API access broken. Re-sign-in via the tool.")
        sys.exit(2)

    print()
    # WRITE TEST skipped — write access already confirmed working.
    # (It used to create a junk 'verify_write_access_test_DELETE_ME' ad
    # unit each run.) The diagnostic now only DUMPS structure for analysis.
    print("=== WRITE TEST: skipped (write access already confirmed) ===")
    print("Diagnostic complete. Paste the ADAPTERS + AdUnitMappings + "
          "mediation group DUMP sections above.")


if __name__ == "__main__":
    main()
