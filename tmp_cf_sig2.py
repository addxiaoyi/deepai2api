import inspect
from camoufox.sync_api import Camoufox

print("raw signature:", str(inspect.signature(Camoufox.__init__)), "\n")
# 看是不是 **kwargs
src = inspect.getsource(Camoufox.__init__)
print(src[:1200])
