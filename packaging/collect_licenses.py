"""Copy the licence files of everything the built app bundles into one folder.

Run in the build environment after PyInstaller:

    python packaging/collect_licenses.py dist/Photo2PDF/licenses build/tesseract

Covers the runtime Python packages (requirements.txt and their dependencies,
taken from the installed metadata, so the files match the pinned versions),
the Python interpreter, Tcl/Tk, and Tesseract. Writes INDEX.txt listing each
component, its version and its declared licence. Fails if a component has no
licence file, so a release never ships without the notices.
"""

from __future__ import annotations

import re
import shutil
import sys
import urllib.request
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
LICENSE_NAME = re.compile(r"(licen[cs]e|copying|notice|authors)[^/]*?(\.(txt|md|rst|terms))?$", re.I)
TEXT_SUFFIXES = {"", ".txt", ".md", ".rst", ".terms"}
TESSERACT_LICENSE_URL = "https://raw.githubusercontent.com/tesseract-ocr/tesseract/main/LICENSE"


def runtime_distributions() -> list[metadata.Distribution]:
    """requirements.txt plus everything they require (markers evaluated here)."""
    todo = []
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            todo.append(Requirement(line).name)
    seen: dict[str, metadata.Distribution] = {}
    while todo:
        name = canonicalize_name(todo.pop())
        if name in seen:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue  # optional dependency not installed, so not bundled
        seen[name] = dist
        for spec in dist.requires or []:
            req = Requirement(spec)
            if req.marker is None or req.marker.evaluate({"extra": ""}):
                todo.append(req.name)
    return sorted(seen.values(), key=lambda d: canonicalize_name(d.metadata["Name"]))


def declared_licence(dist: metadata.Distribution) -> str:
    m = dist.metadata
    if m.get("License-Expression"):
        return m["License-Expression"]
    classifiers = [c.split("::")[-1].strip() for c in m.get_all("Classifier") or []
                   if c.startswith("License ::")]
    if classifiers:
        return "; ".join(classifiers)
    lic = (m.get("License") or "").strip()
    return lic.splitlines()[0][:80] if lic else "see files"


def is_licence_file(rel: str) -> bool:
    p = Path(rel)
    if p.suffix.lower() not in TEXT_SUFFIXES:
        return False
    return bool(LICENSE_NAME.search(p.name)) or "licenses" in p.parts


def copy_dist(dist: metadata.Distribution, out: Path) -> int:
    """Copy each distinct licence text once (wheels often ship the same file twice)."""
    import hashlib

    seen: set[str] = set()
    for f in dist.files or []:
        rel = str(f)
        src = Path(dist.locate_file(f))
        if not is_licence_file(rel) or not src.is_file():
            continue
        digest = hashlib.sha256(src.read_bytes()).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        dst = out / Path(rel).name
        if dst.exists():
            dst = out / rel.replace("/", "_")
        shutil.copyfile(src, dst)
    return len(seen)


def main(out_dir: str, tess_dir: str) -> int:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    index, missing = [], []

    for dist in runtime_distributions():
        name, ver = dist.metadata["Name"], dist.version
        d = out / f"{canonicalize_name(name)}-{ver}"
        d.mkdir(exist_ok=True)
        if copy_dist(dist, d) == 0:
            missing.append(name)
        index.append(f"{name} {ver}: {declared_licence(dist)}")

    py = out / f"python-{sys.version.split()[0]}"
    py.mkdir(exist_ok=True)
    base = Path(sys.base_prefix)
    for cand in ("LICENSE.txt", "LICENSE"):
        if (base / cand).is_file():
            shutil.copyfile(base / cand, py / "LICENSE.txt")
            break
    else:
        missing.append("Python")
    index.append(f"Python {sys.version.split()[0]}: PSF-2.0")

    tcl = sorted(base.glob("tcl/*/license.terms"))
    if tcl:
        d = out / "tcl-tk"
        d.mkdir(exist_ok=True)
        for f in tcl:
            shutil.copyfile(f, d / f"{f.parent.name}-license.terms")
        index.append("Tcl/Tk: TCL (BSD-style)")

    tess = Path(tess_dir)
    d = out / "tesseract"
    d.mkdir(exist_ok=True)
    found = [f for f in tess.iterdir() if f.is_file() and LICENSE_NAME.search(f.name)] if tess.is_dir() else []
    for f in found:
        shutil.copyfile(f, d / f.name)
    if not found:
        with urllib.request.urlopen(TESSERACT_LICENSE_URL, timeout=60) as r:
            (d / "LICENSE").write_bytes(r.read())
    index.append("Tesseract OCR and tessdata_fast language data (eng, nld, osd): Apache-2.0")

    (out / "INDEX.txt").write_text(
        "Third-party components bundled with Photos to PDF.\n"
        "Each folder holds the licence files shipped by that component.\n\n"
        + "\n".join(index) + "\n", encoding="utf-8")
    print("\n".join(index))
    if missing:
        print(f"ERROR: no licence file found for: {', '.join(missing)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
