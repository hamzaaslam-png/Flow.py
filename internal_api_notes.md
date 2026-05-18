# AdMob Internal API — reverse-engineering notes

NOT the public API. This is the undocumented internal API the AdMob
web console uses. Auth = browser session cookies + x-framework-xsrf-token.

## Endpoints

- List (read):
  `POST https://admob.google.com/v2/mediationAllocation/_/rpc/MediationAllocationService/List`
- Save mediation group (create/update):
  `POST https://admob.google.com/v2/mediationGroup/_/rpc/MediationGroupService/V2Update`

Query params: `?authuser=0&authuser=0&f.sid=<session id>`

## Required headers

- `content-type: application/x-www-form-urlencoded`
- `activityname: MediationGroup.Save` (varies per call)
- `appname: tlc`
- `origin: https://admob.google.com`
- `x-framework-xsrf-token: <token>:<timestamp ms>`
- `x-same-domain: 1`
- Cookie blob (the Google login session: SID, SAPISID, __Secure-1PSID, etc.)

## Body = `f.req=<url-encoded JSON>` (protobuf-style numbered fields)

V2Update body decoded — a mediation group:

```
{ "1": {
  "1": "<mediationGroupId>",          # group id ("" or omit for new)
  "2": "<display name>",
  "3": 1,                              # state (1 = enabled)
  "4": {                               # targeting
    "1": <platform>,                   # 2 = Android
    "2": <format>,                     # 7 = App Open
    "3": ["<ad unit id>", ...]         # targeted ad units
  },
  "5": [ <lines...> ],                 # mediation group lines
  "10": {"1": 0}, "14": {}, "15": 0,
  "16": {"1": false}, "17": false
} }
```

### Line: AdMob Network (LIVE bidding) — auto-added
```
{ "1":"<lineId>", "2":"1", "3":1, "4":1,
  "5":{"1":"10000","2":"USD"}, "6":false,
  "9":"AdMob Network", "11":1, "14":"1" }
```

### Line: AdMob Network WATERFALL (the target!)
```
{ "2":"402",                          # ad source code for AdMob Network Waterfall
  "3":8,
  "4":2,                              # 2 = MANUAL  (1 = bidding/LIVE)
  "5":{"1":"5000000","2":"USD"},      # eCPM in micros (5000000 = $5.00)
  "9":"AdMob Network Waterfall",
  "11":1,
  "13":["<backing placement id>"],    # <-- HOW IS THIS OBTAINED? (open question)
  "14":"616" }                        # adapter id (616 = App Open Android waterfall)
```
NOTE: the waterfall line has NO field "1" (no line id => new) and
NO field "7" (no 3rd-party placement config — AdMob's own inventory).

### Line: 3rd-party (Pangle/Mintegral/Liftoff bidding) — for reference
```
{ "1":"<lineId>", "2":"<code>", "3":8, "4":1,
  "5":{"1":"10000","2":"USD"},
  "7":[{ "1":"<adUnitId>",
         "2":{"1":[{"1":"placementid","2":"..."},...]},
         "5":"App open", "6":"<mappingId>" }],
  "9":"Pangle (bidding)", "11":1, "12":{"1":0},
  "13":["<mappingId>"], "14":"<adapterCode>" }
```

## Adapter codes (AdMob Network Waterfall source = 1215381445328257950)
504 Banner/Interstitial Android | 505 iOS
506 Rewarded Android | 507 iOS
508 Native Android | 509 iOS
510 Rewarded-Interstitial Android | 511 iOS
616 App Open Android | 617 iOS

## OPEN QUESTION — the one missing piece

Field "13" of the AdMob Network Waterfall line = `["1121831348319602"]` —
a backing placement id. Need to capture the request that CREATES this id
(fires when you "Add ad source -> AdMob Network" to the waterfall, before
the final Save). Likely a separate rpc call. Once known, the full
create-waterfall-line flow can be replicated.

## Auth reality

Cookie-based (browser session). Not OAuth. Cookies expire — any tool
calling this API needs fresh cookies periodically, OR a server-side
headless browser. This is the fragile part.
