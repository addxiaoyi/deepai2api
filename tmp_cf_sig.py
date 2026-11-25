import inspect, sys
from camoufox.sync_api import Camoufox
sig = inspect.signature(Camoufox.__init__)
print("camoufox 0.5.9  Camoufox.__init__ 真实参数：\n")
for name, p in sig.parameters.items():
    if name == "self":
        continue
    default = "" if p.default is inspect._empty else f" = {p.default!r}"
    print(f"  {name}{default}")
