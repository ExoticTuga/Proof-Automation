import inspect
import isabelle_lsp_client as m

targets = [
    ("IsabelleProcess", ["__init__", "run", "start_isabelle", "write_loop"]),
    ("IsabelleClient",  ["__init__", "initialize", "open_text_document",
                         "caret_update", "close_text_document", "shutdown", "exit"]),
    ("ClientHandler",   ["__init__", "register_on_dynamic_output",
                         "register_on_start", "add_document", "set_document", "handle"]),
    ("Document",        ["__init__", "open_file", "move_caret", "get_progress"]),
]

for cls_name, methods in targets:
    cls = getattr(m, cls_name, None)
    if cls is None:
        print(f"{cls_name}: MISSING\n")
        continue
    print(f"=== {cls_name}")
    for meth in methods:
        fn = getattr(cls, meth, None)
        if fn is None:
            print(f"  {meth}: absent")
            continue
        try:
            print(f"  {meth}{inspect.signature(fn)}")
        except (ValueError, TypeError) as e:
            print(f"  {meth}: <{e}>")
    print()

for model in ("DynamicOutput", "CaretUpdateRequest"):
    cls = getattr(m, model, None)
    if cls is not None:
        print(f"=== {model} fields")
        for name, f in cls.model_fields.items():
            print(f"  {name}: {f.annotation}")
        print()
