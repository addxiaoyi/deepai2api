import inspect
from camoufox import utils
src = inspect.getsource(utils.launch_options)
start = src.find("fingerprint_preset (")
print(src[start:start+700])
print("=" * 60)
start = src.find("i_know_what_im_doing (")
print(src[start:start+600])
