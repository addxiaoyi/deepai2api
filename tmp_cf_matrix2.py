from camoufox.sync_api import Camoufox
from camoufox.fingerprints import Screen
import warnings
warnings.filterwarnings("ignore")

def probe(label, **launch):
    launch.setdefault("headless", True)
    launch.setdefault("i_know_what_im_doing", True)
    try:
        with Camoufox(**launch) as browser:
            page = browser.new_page(); page.goto("about:blank")
            print(f"[OK]   {label}: os={page.evaluate('navigator.platform')} "
                  f"screen={page.evaluate('[screen.width,screen.height]')} "
                  f"tz={page.evaluate('Intl.DateTimeFormat().resolvedOptions().timeZone')} "
                  f"cores={page.evaluate('navigator.hardwareConcurrency')}")
            return True
    except Exception as exc:
        print(f"[FAIL] {label}: {type(exc).__name__}: {str(exc)[:130]}")
        return False

# 1. 只 os
probe("只 os=['windows','macos']", os=["windows","macos"])
# 2. os + window（不传 screen）
probe("os + window=(1366,768)", os=["windows","macos"], window=(1366,768))
# 3. os + screen（不传 window）
probe("os + screen(1280..1920,720..1080)", os=["windows","macos"],
      screen=Screen(min_width=1280,min_height=720,max_width=1920,max_height=1080))
# 4. os + screen + window（我原来的组合）→ 预期失败
probe("os + screen + window=(1366,768) ← 原组合", os=["windows","macos"],
      screen=Screen(min_width=1280,min_height=720,max_width=1920,max_height=1080), window=(1366,768))
# 5. screen 下限抬高到 1366x768，再配同尺寸 window
probe("os + screen(1366..1920,768..1080) + window(1366,768)",
      os=["windows","macos"],
      screen=Screen(min_width=1366,min_height=768,max_width=1920,max_height=1080), window=(1366,768))
# 6. 只用 windows + screen + window
probe("os=['windows'] + screen(1366..1920,768..1080) + window(1366,768)",
      os=["windows"],
      screen=Screen(min_width=1366,min_height=768,max_width=1920,max_height=1080), window=(1366,768))
