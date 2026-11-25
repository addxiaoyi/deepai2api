import time
t0 = time.time()
try:
    from camoufox.sync_api import Camoufox
    from camoufox.fingerprints import Screen

    # 我当前 worker 里的参数集（原样复刻）——目的是看它到底能不能启动
    launch = dict(
        headless=True,
        humanize=2.2,
        os=["windows", "macos"],
        locale=["en-US"],
        block_webrtc=True,
        fingerprint_preset=True,
        screen=Screen(min_width=1280, min_height=720, max_width=1920, max_height=1080),
        window=(1366, 768),
        pin_cpu_cores=True,
        enable_cache=True,
        geoip=False,
        i_know_what_im_doing=True,
        timezone="America/New_York",   # ← 可疑参数
    )
    print("launching with current arg set ...")
    with Camoufox(**launch) as browser:
        page = browser.new_page()
        page.goto("about:blank")
        ua = page.evaluate("navigator.userAgent")
        cores = page.evaluate("navigator.hardwareConcurrency")
        tz = page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone")
        print("OK ua:", ua[:80])
        print("   cores:", cores, "tz:", tz)
except Exception as exc:
    print(f"FAILED: {type(exc).__name__}: {str(exc)[:600]}")
finally:
    print(f"elapsed {time.time()-t0:.1f}s")
