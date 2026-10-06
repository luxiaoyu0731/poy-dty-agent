"""Build a traced runtime filesystem from an exact, locally available image.

Run only in the disposable Docker build stage. No downloads, package upgrades,
production mounts or dependency resolution. Every copied file is fingerprinted.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path("/runtime")
COPIED: dict[str, str] = {}
OPTIONAL_INTERACTIVE = re.compile(r"^(_curses(?:_panel)?|readline|_uuid|_tkinter)\..*\.so$")


def copy_file(path: Path) -> None:
    if not path.is_file():
        return
    target = ROOT / path.relative_to("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target, follow_symlinks=True)
    COPIED[str(path)] = str(path.resolve())


def main() -> None:
    if ROOT.exists():
        raise RuntimeError("runtime_destination_must_be_new")
    ROOT.mkdir()
    for base in ("/usr/local", "/app", "/etc/ssl/certs", "/usr/share/zoneinfo"):
        for path in Path(base).rglob("*"):
            if "__pycache__" in path.parts or path.suffix == ".pyc" or OPTIONAL_INTERACTIVE.match(path.name):
                continue
            # The frozen application venv has include-system-site-packages=false.
            # Global pip/uv/setuptools are build tools, never runtime imports.
            if path.is_relative_to("/usr/local/lib/python3.11/site-packages"):
                continue
            if path.parent == Path("/usr/local/bin") and path.name.startswith(("pip", "uv", "wheel")):
                continue
            copy_file(path)
    for name in ("/bin/sh", "/usr/bin/env", "/etc/passwd", "/etc/group", "/etc/nsswitch.conf",
                 "/etc/os-release", "/etc/debian_version", "/etc/ssl/openssl.cnf"):
        copy_file(Path(name))
    # Discover dependencies using the real ELF files, including wheel extensions.
    # Excluded modules provide optional terminal UI/OS uuid acceleration only;
    # uuid.py retains its documented pure-Python fallback. Server imports are tested.
    for original in list(COPIED):
        path = Path(original)
        with path.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                continue
        result = subprocess.run(["ldd", str(path)], capture_output=True, text=True, check=False)
        output = result.stdout + result.stderr
        if "not found" in output:
            raise RuntimeError(f"unresolved_runtime_dependency:{path}:{output[:300]}")
        for match in re.findall(r"(/[^\s()]+)", output):
            library = Path(match)
            if library.is_file() and not str(library).startswith("/runtime/"):
                copy_file(library)
    # Keep the *actual* Debian package metadata for copied components. Do not
    # remove registrations for code that remains in this filesystem.
    originals = set(COPIED) | set(COPIED.values())
    owners = set()
    for listing in Path("/var/lib/dpkg/info").glob("*.list"):
        if any(line in originals or (Path(line).exists() and str(Path(line).resolve()) in originals)
               for line in listing.read_text().splitlines()):
            owners.add(listing.name.removesuffix(".list").split(":")[0])
    records = Path("/var/lib/dpkg/status").read_text().split("\n\n")
    selected = [record for record in records if any(record.startswith(f"Package: {owner}\n") for owner in owners)]
    status = ROOT / "var/lib/dpkg/status"
    status.parent.mkdir(parents=True)
    status.write_text("\n\n".join(selected) + "\n")
    for directory in ("data", "tmp"):
        (ROOT / directory).mkdir(exist_ok=True)
    os.chmod(ROOT / "tmp", 0o1777)
    os.chown(ROOT / "data", 10001, 10001)
    manifest = {"copied_packages": sorted(owners), "removed_optional_modules": OPTIONAL_INTERACTIVE.pattern,
                "files": [{"path": path, "original": original,
                           "sha256": hashlib.sha256((ROOT / path.lstrip("/")).read_bytes()).hexdigest()}
                          for path, original in sorted(COPIED.items())]}
    (ROOT / "runtime-manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2))
    print(json.dumps({"copied_files": len(COPIED), "copied_packages": sorted(owners)}))


if __name__ == "__main__":
    main()
