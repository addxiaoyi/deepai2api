import camoufox, pkgutil, inspect, pathlib
root = pathlib.Path(camoufox.__file__).parent
print("package:", root, "\n")
# 找合法 option 列表
import camoufox.utils as U
print("utils members:", [n for n in dir(U) if "option" in n.lower() or "launch" in n.lower()])
for name in dir(U):
    if "OPTION" in name.upper() or "LAUNCH" in name.upper():
        val = getattr(U, name)
        if isinstance(val, (list, tuple, set, dict)):
            print(f"\n{name} ({type(val).__name__}, {len(val)}):")
            items = list(val.keys()) if isinstance(val, dict) else list(val)
            for it in items:
                print("  ", it)
