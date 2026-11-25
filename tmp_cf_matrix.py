import time
from camoufox.sync_api import Camoufox
from camoufox.fingerprints import Screen

def probe(label, **launch):
    t0 = time.time()
    try:
        with Camoufox(**launch) as browser:
            page = browser.new_page()
            page.goto("about:blank")
            info = {
                "ua": page.evaluate("navigator.userAgent")[:60],
                "cores": page.evaluate("navigator.hardwareConcurrency"),
                "tz": page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone"),
                "lang": page.evaluate("navigator.language"),
                "w": page.evaluate("window.screen.width"),
            }
        print(f"[OK]   {label}: {info}  ({time.time()-t0:.1f}s)")
        return True
    except Exception as exc:
        print(f"[FAIL] {label}: {type(exc).__name__}: {str(exc)[:200]}  ({time.time()-t0:.1f}s)")
        return False

base = dict(headless=True, humanize=2.2, block_webrtc=True, enable_cache=True,
            screen=Screen(min_width=1280, min_height=720, max_width=1920, max_height=1080),
            window=(1366, 768))

probe("A 修正后最小集（无 timezone / 无 preset）", os=["windows","macos"], locale=["en-US"], **base)
probe("B 加 fingerprint_preset=True", os=["windows","macos"], locale=["en-US"], fingerprint_preset=True, **base)
probe("C 加 pin_cpu_cores=True", os=["windows","macos"], locale=["en-US"], pin_cpu_cores=True, **base)
probe("D 加 geoip=True", os=["windows","macos"], locale=["en-US"], geoip=True, **base)
