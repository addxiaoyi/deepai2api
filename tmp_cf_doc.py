import inspect
from camoufox import utils
src = inspect.getsource(utils.launch_options)
# 找到 geoip / timezone / locale 的说明段
start = src.find("geoip (")
print(src[start:start+1800])
