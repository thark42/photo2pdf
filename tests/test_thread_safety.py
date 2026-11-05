"""Worker threads must never reference a window (self / app / root).

If a worker thread drops the last reference to a Tk object, Tk is torn down from that
thread; on Windows that aborts the process ("Tcl_AsyncDelete: async handler deleted by
the wrong thread", seen in CI). Worker bodies may only use plain data: queues, events,
paths, options."""
import ast
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "photo2pdf"


def _thread_targets(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "Thread":
            for kw in node.keywords:
                if kw.arg == "target":
                    if isinstance(kw.value, ast.Name):
                        names.add(kw.value.id)
                    elif isinstance(kw.value, ast.Lambda):
                        yield ("<lambda>", kw.value)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            yield (node.name, node)


def test_no_window_references_in_worker_threads():
    offenders = []
    for src in ("app.py", "review.py"):
        tree = ast.parse((PKG / src).read_text("utf-8"))
        for name, fn in _thread_targets(tree):
            for n in ast.walk(fn):
                if isinstance(n, ast.Name) and n.id in ("self", "app", "root"):
                    offenders.append(f"{src}:{name}:{n.lineno}")
    assert not offenders, offenders
