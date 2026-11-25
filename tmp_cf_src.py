import inspect, re
from camoufox import utils
src = inspect.getsource(utils.launch_options)
# 打印完整源码里 fingerprint 段落
idx = src.find('fingerprint')
print(src[:2500])
