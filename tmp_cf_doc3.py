import inspect
from camoufox import utils
src = inspect.getsource(utils.launch_options)
for key in ("i_know_what_im_doing", "pin_cpu_cores", "canvas_noise", "main_world_eval"):
    i = src.find(key + " (")
    print(src[i:i+420] if i != -1 else f"{key}: (无文档段)")
    print("-" * 60)
