from camoufox.utils import launch_options, new_context_options, persistent_context_options
import inspect

for fn in (launch_options, new_context_options, persistent_context_options):
    print("=" * 70)
    print(fn.__name__, inspect.signature(fn))
    print()
