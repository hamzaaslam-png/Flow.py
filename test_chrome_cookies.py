"""Probe whether we can read this machine's Chrome cookies for Google.

The AdMob internal API needs the browser login cookies (SID, SAPISID,
__Secure-1PSID, ...). Newer Chrome encrypts cookies with app-bound
encryption, which can block extraction. This script tries two libraries
and reports exactly what works.

Run:  python test_chrome_cookies.py
"""
from __future__ import annotations

KEY_COOKIES = ["SID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID",
               "HSID", "SSID", "APISID"]


def _looks_decrypted(value: str) -> bool:
    """A correctly decrypted Google auth cookie is a long printable string."""
    return bool(value) and len(value) > 10 and value.isprintable()


def try_browser_cookie3():
    print("=" * 64)
    print("browser_cookie3")
    print("-" * 64)
    try:
        import browser_cookie3 as bc3
    except Exception as e:
        print(f"  import failed: {e}")
        return {}
    found: dict[str, str] = {}
    try:
        cj = bc3.chrome(domain_name="google.com")
        for c in cj:
            found[c.name] = c.value or ""
    except Exception as e:
        print(f"  read failed: {type(e).__name__}: {e}")
        return {}
    print(f"  total google.com cookies read: {len(found)}")
    for k in KEY_COOKIES:
        v = found.get(k)
        if v is None:
            print(f"  {k:18s}: MISSING")
        else:
            ok = _looks_decrypted(v)
            print(f"  {k:18s}: {'OK  len=' + str(len(v)) if ok else 'GARBAGE/empty'}")
    return found


def try_rookiepy():
    print("=" * 64)
    print("rookiepy")
    print("-" * 64)
    try:
        import rookiepy
    except Exception as e:
        print(f"  import failed: {e}")
        return {}
    found: dict[str, str] = {}
    try:
        cookies = rookiepy.chrome([".google.com", "admob.google.com",
                                   "google.com"])
        for c in cookies:
            found[c["name"]] = c.get("value", "") or ""
    except Exception as e:
        print(f"  read failed: {type(e).__name__}: {e}")
        return {}
    print(f"  total google cookies read: {len(found)}")
    for k in KEY_COOKIES:
        v = found.get(k)
        if v is None:
            print(f"  {k:18s}: MISSING")
        else:
            ok = _looks_decrypted(v)
            print(f"  {k:18s}: {'OK  len=' + str(len(v)) if ok else 'GARBAGE/empty'}")
    return found


def verdict(bc3_found: dict, rookie_found: dict):
    print("=" * 64)
    print("VERDICT")
    print("-" * 64)

    def score(found: dict) -> int:
        return sum(1 for k in ("SID", "SAPISID", "__Secure-1PSID")
                   if _looks_decrypted(found.get(k, "")))

    s_bc3, s_rk = score(bc3_found), score(rookie_found)
    if s_bc3 == 3:
        print(">>> browser_cookie3 WORKS — all 3 critical cookies decrypted.")
        print(">>> flow.py can read Chrome cookies automatically with it.")
    elif s_rk == 3:
        print(">>> rookiepy WORKS — all 3 critical cookies decrypted.")
        print(">>> flow.py can read Chrome cookies automatically with rookiepy.")
    elif s_bc3 or s_rk:
        print(f">>> PARTIAL — browser_cookie3 got {s_bc3}/3, rookiepy got {s_rk}/3.")
        print(">>> Some cookies decrypt; app-bound encryption may block the rest.")
        print(">>> Paste the full output back so we decide the path.")
    else:
        print(">>> NEITHER could decrypt the critical Google cookies.")
        print(">>> Chrome's app-bound encryption is blocking it on this machine.")
        print(">>> We fall back to the paste-once approach. Paste output back.")


if __name__ == "__main__":
    bc3_found = try_browser_cookie3()
    print()
    rookie_found = try_rookiepy()
    print()
    verdict(bc3_found, rookie_found)
