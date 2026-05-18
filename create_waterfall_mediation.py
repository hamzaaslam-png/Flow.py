"""
Create an AdMob mediation group with AdMob Network Waterfall lines —
using AdMob's INTERNAL web API (admob.google.com/v2/...).

This is the SAME API the AdMob website itself uses. It is NOT the public
developer API. It is what ADFLUX-style tools use.

⚠️  IMPORTANT — read before using:
  - This is an UNDOCUMENTED internal API. Google can change it anytime.
  - Auth = your browser session cookies. Cookies EXPIRE (hours/days) —
    when they do, re-capture and update the CONFIG below.
  - Automating AdMob's web interface is a grey area w.r.t. AdMob Terms
    of Service. Use on your own account, at your own risk.

────────────────────────────────────────────────────────────────────────
HOW THE WATERFALL IS BUILT (two phases)
────────────────────────────────────────────────────────────────────────
The console does NOT create a waterfall line in one shot. It first
creates a "backing placement" for the AdMob Network Waterfall ad source,
then references that placement's id (field "13") on the mediation-group
line. So this script does:

  PHASE 1  — for each eCPM tier, POST MediationAllocationService/V2Update
             (activity CreateMappingInEditModal). Each call returns a
             backing-placement id.
  PHASE 2  — POST MediationGroupService/V2Update with one waterfall line
             per tier, each line carrying its placement id in field "13".

HOW TO USE
 1. Open admob.google.com in Chrome, logged into YOUR account.
 2. DevTools -> Network. Do any action; find a request to .../rpc/...
 3. Copy as cURL (bash), then copy THREE values into CONFIG:
      COOKIE     : text after  -b '...'
      XSRF_TOKEN : header  x-framework-xsrf-token
      F_SID      : the  f.sid=...  value in the URL
 4. Fill the GROUP / WATERFALL settings below.
 5. Run:  python create_waterfall_mediation.py
 6. Read the printed output. Paste it back if anything fails — the
    response almost always says exactly what to adjust.
────────────────────────────────────────────────────────────────────────
"""
import json
import urllib.parse
import urllib.request
import urllib.error

# ════════════════════════════════════════════════════════════════════
# CONFIG  — paste these 3 values from a freshly captured cURL
# ════════════════════════════════════════════════════════════════════
# ⚠️ LIVE session credentials. They EXPIRE (likely within a day). Do NOT
# commit this file to git / share it. On 401/403, capture a fresh cURL.
COOKIE = """PASTE_COOKIE_HERE"""

XSRF_TOKEN = "PASTE_XSRF_TOKEN_HERE"

F_SID = "PASTE_F_SID_HERE"

# ════════════════════════════════════════════════════════════════════
# WHAT TO CREATE
# ════════════════════════════════════════════════════════════════════
# Leave GROUP_ID empty ("") to create a NEW group.
GROUP_ID = ""

GROUP_NAME = "Test_AdMob_Waterfall"

# platform code:  2 = Android   (1 = iOS — verify if you need iOS)
PLATFORM = 2

# Targeting ad-format code (mediation-group targeting field "2").
#   7 = App Open (confirmed from capture). Others unverified.
AD_FORMAT = 7

# Line / placement format code (mediation-group line field "3", and
# placement field "10"). 8 was observed alongside AD_FORMAT 7.
LINE_FORMAT_CODE = 8

# Ad unit id(s) the mediation group serves — SHORT numeric id.
TARGET_AD_UNITS = ["3722795925"]

# The "AdMob Network Waterfall" backing ad unit, in ca-app-pub form.
# This is the "pubid" config value seen in the CreateMappingInEditModal
# capture (e.g. ca-app-pub-3634329033658630/2289230954). It is the ad
# unit AdMob's own waterfall inventory serves from for these tiers.
PLACEMENT_PUBID = "ca-app-pub-3634329033658630/2289230954"

# "AdMob Network Waterfall" adapter code — pick by format + platform:
#   504 = Banner/Interstitial Android   505 = iOS
#   506 = Rewarded Android              507 = iOS
#   508 = Native Android                509 = iOS
#   510 = Rewarded-Interstitial Android 511 = iOS
#   616 = App Open Android              617 = iOS
WATERFALL_ADAPTER = "616"

# Waterfall eCPM tiers, USD, highest first. One backing placement and
# one mediation-group line is created per tier.
WATERFALL_ECPMS = [5.00, 4.00, 3.00, 2.00, 1.00]

# Also add the AdMob Network LIVE bidding line?
INCLUDE_ADMOB_BIDDING = True

# Phase 1 only: create the backing placements, print the raw responses,
# and STOP (do not touch the mediation group). Use this the first time
# to confirm the response shape before letting Phase 2 run.
PHASE1_ONLY = False
# ════════════════════════════════════════════════════════════════════

ALLOC_URL = ("https://admob.google.com/v2/mediationAllocation/_/rpc/"
             "MediationAllocationService/V2Update")
GROUP_URL = ("https://admob.google.com/v2/mediationGroup/_/rpc/"
             "MediationGroupService/V2Update")


def _config_ready() -> bool:
    return not any("PASTE_" in v for v in (COOKIE, XSRF_TOKEN, F_SID))


def _strip_xssi(text: str) -> str:
    """Google RPC responses are often prefixed with )]}' to block XSSI."""
    t = text.lstrip()
    if t.startswith(")]}'"):
        nl = t.find("\n")
        t = t[nl + 1:] if nl != -1 else t[4:]
    return t.strip()


def post_rpc(url: str, activity: str, body_obj: dict) -> tuple[int, str]:
    """POST one f.req call to the internal API. Returns (status, text)."""
    f_req = json.dumps(body_obj, separators=(",", ":"))
    post_data = "f.req=" + urllib.parse.quote(f_req, safe="")
    full = f"{url}?authuser=0&authuser=0&f.sid={F_SID}"

    req = urllib.request.Request(full, data=post_data.encode("utf-8"),
                                 method="POST")
    req.add_header("content-type", "application/x-www-form-urlencoded")
    req.add_header("cookie", COOKIE.strip())
    req.add_header("x-framework-xsrf-token", XSRF_TOKEN.strip())
    req.add_header("x-same-domain", "1")
    req.add_header("origin", "https://admob.google.com")
    req.add_header("referer",
                   "https://admob.google.com/v2/mediation/groups/list")
    req.add_header("appname", "tlc")
    req.add_header("activityname", activity)
    req.add_header("accept", "*/*")
    req.add_header("user-agent",
                   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/146.0.0.0 Safari/537.36")
    try:
        resp = urllib.request.urlopen(req, timeout=40)
        return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def build_placement_body() -> dict:
    """CreateMappingInEditModal body — one AdMob Network Waterfall
    backing placement on the target ad unit."""
    return {"1": [{
        "1": "-1",                 # -1 => create new
        "2": True,
        "3": "402",                # ad source code: AdMob Network Waterfall
        "4": [{"1": "pubid", "2": PLACEMENT_PUBID}],
        "10": LINE_FORMAT_CODE,
        "12": TARGET_AD_UNITS[0],
        "16": WATERFALL_ADAPTER,
    }]}


def extract_placement_id(parsed) -> str | None:
    """Walk the V2Update response and find the created placement id —
    an object that carries the AdMob Network Waterfall source code "402"
    and a numeric id in field "1" that is not the "-1" placeholder."""
    hits: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            v1 = node.get("1")
            if node.get("3") == "402" and isinstance(v1, str) \
                    and v1.isdigit() and v1 != "-1":
                hits.append(v1)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(parsed)
    return hits[0] if hits else None


def create_backing_placement(tier_idx: int, ecpm: float) -> str | None:
    """Phase 1: create one backing placement, return its id (or None)."""
    body = build_placement_body()
    print(f"[Phase 1] tier {tier_idx} (${ecpm:.2f}) — "
          f"POST MediationAllocationService/V2Update")
    print("  f.req:", json.dumps(body, separators=(",", ":")))
    status, text = post_rpc(ALLOC_URL, "MediationGroup.CreateMappingInEditModal",
                            body)
    print(f"  HTTP {status}")
    snippet = text[:1500]
    print("  response:", snippet)
    if status not in (200, 201):
        print("  >>> placement create FAILED — see response above.")
        return None
    try:
        parsed = json.loads(_strip_xssi(text))
    except json.JSONDecodeError:
        print("  >>> response was not JSON — paste full output back.")
        return None
    pid = extract_placement_id(parsed)
    if pid:
        print(f"  >>> backing placement id = {pid}")
    else:
        print("  >>> could not locate placement id in response — paste "
              "full output back so the parser can be adjusted.")
    print()
    return pid


def build_waterfall_line(ecpm_usd: float, placement_id: str) -> dict:
    """One AdMob Network Waterfall MANUAL line, wired to its placement."""
    micros = str(int(round(ecpm_usd * 1_000_000)))
    return {
        "2": "402",                       # ad source: AdMob Network Waterfall
        "3": LINE_FORMAT_CODE,
        "4": 2,                           # 2 = MANUAL (waterfall)
        "5": {"1": micros, "2": "USD"},   # eCPM floor
        "9": "AdMob Network Waterfall",
        "11": 1,
        "13": [placement_id],             # the backing placement from Phase 1
        "14": WATERFALL_ADAPTER,
    }


def build_group_body(tier_placements: list[tuple[float, str]]) -> dict:
    lines = []

    if INCLUDE_ADMOB_BIDDING:
        lines.append({
            "2": "1", "3": 1, "4": 1,
            "5": {"1": "10000", "2": "USD"}, "6": False,
            "9": "AdMob Network", "11": 1, "14": "1",
        })

    for ecpm, pid in tier_placements:
        lines.append(build_waterfall_line(ecpm, pid))

    group = {
        "2": GROUP_NAME,
        "3": 1,                            # state ENABLED
        "4": {                             # targeting
            "1": PLATFORM,
            "2": AD_FORMAT,
            "3": list(TARGET_AD_UNITS),
        },
        "5": lines,
        "10": {"1": 0},
        "14": {},
        "15": 0,
        "16": {"1": False},
        "17": False,
    }
    if GROUP_ID:
        group["1"] = GROUP_ID
    return {"1": group}


def main():
    if not _config_ready():
        print("ERROR: fill COOKIE / XSRF_TOKEN / F_SID in the CONFIG block "
              "first (capture them from a cURL in DevTools).")
        return

    print("=" * 70)
    print("PHASE 1 — create backing placements (one per eCPM tier)")
    print("=" * 70)
    tier_placements: list[tuple[float, str]] = []
    for i, ecpm in enumerate(WATERFALL_ECPMS, start=1):
        pid = create_backing_placement(i, ecpm)
        if not pid:
            print("Aborting: a backing placement could not be created.")
            print("Fix the issue above (or paste the output back) and rerun.")
            return
        tier_placements.append((ecpm, pid))

    print(f"Phase 1 done — {len(tier_placements)} backing placement(s):")
    for ecpm, pid in tier_placements:
        print(f"  ${ecpm:.2f} -> {pid}")
    print()

    if PHASE1_ONLY:
        print("PHASE1_ONLY is set — stopping before touching the mediation "
              "group. Set PHASE1_ONLY = False to run Phase 2.")
        return

    print("=" * 70)
    print("PHASE 2 — save mediation group with waterfall lines")
    print("=" * 70)
    body_obj = build_group_body(tier_placements)
    print("f.req (decoded):")
    print(json.dumps(body_obj, indent=2))
    print("-" * 70)
    status, text = post_rpc(GROUP_URL, "MediationGroup.Save", body_obj)
    print(f"RESULT: HTTP {status}")
    print(text[:4000])
    print()
    if status in (200, 201):
        print(">>> If this shows the created group / a group id — SUCCESS.")
        print(">>> Check admob.google.com -> Mediation to confirm.")
    elif status in (401, 403):
        print(">>> 401/403 = cookies/xsrf expired. Capture a fresh cURL "
              "and update the CONFIG block.")
    else:
        print(">>> Paste this full output back for analysis.")


if __name__ == "__main__":
    main()
