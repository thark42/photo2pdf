"""The app must never touch the network. Guard against someone adding it."""
import ast
from pathlib import Path

FORBIDDEN = {"socket", "urllib", "http", "requests", "httpx", "aiohttp", "ftplib",
             "smtplib", "webbrowser", "ssl"}


def test_no_network_imports():
    pkg = Path(__file__).resolve().parent.parent / "photo2pdf"
    for src in pkg.glob("*.py"):
        tree = ast.parse(src.read_text("utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                assert n.split(".")[0] not in FORBIDDEN, f"{src.name} imports {n}"
