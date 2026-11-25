import inspect
from camoufox.fingerprints import Screen
print("Screen 签名:", inspect.signature(Screen))
print()

# fingerprint dict 支持哪些键？
from camoufox import utils
src = inspect.getsource(utils.launch_options)
import re
# 找 fingerprint 相关处理
for m in re.finditer(r".{0,200}fingerprint.{0,300}", src, re.S):
    chunk = m.group(0)
    if "timezone" in chunk.lower() or "def " in chunk:
        print(chunk[:400])
        print("-" * 60)
