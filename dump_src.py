import inspect
import isabelle_lsp_client as m

for cls_name, meths in [
    ("IsabelleProcess", ["run", "write_loop", "on_finished", "start_isabelle"]),
    ("Document", ["__init__", "open_file", "move_caret", "get_progress"]),
]:
    cls = getattr(m, cls_name)
    for meth in meths:
        fn = getattr(cls, meth, None)
        if fn is None:
            continue
        print(f"{'='*70}\n{cls_name}.{meth}\n{'='*70}")
        try:
            print(inspect.getsource(fn))
        except Exception as e:
            print(f"<no source: {e}>")

try:
    from lsp_client.client import LSPClient
    print(f"{'='*70}\nLSPClient.__init__\n{'='*70}")
    print(inspect.signature(LSPClient.__init__))
    print("\nmethods:", [a for a in dir(LSPClient) if not a.startswith('_')])
except Exception as e:
    print("LSPClient import failed:", e)
