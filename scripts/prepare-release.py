"""Prepare versioned source archive, SHA256 and provenance; never publish here."""
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


def prepare(version, output):
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-preview\.[0-9]+)?", version):
        raise ValueError("Use vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-preview.N")
    repo = Path(__file__).resolve().parents[1]
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo).decode().strip()
    if git("status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Tracked files must be clean before preparing a release")
    sha = git("rev-parse", "HEAD")
    root = Path(output).absolute()
    root.mkdir(parents=True, exist_ok=False)
    archive = root / f"source-{version}.zip"
    subprocess.run(["git", "archive", "--format=zip", "--prefix=source/", "-o", str(archive), sha], cwd=repo, check=True)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (root / "SHA256SUMS").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    (root / "provenance.json").write_text(json.dumps({"version":version,"commit":sha,"artifact":archive.name,"sha256":digest,"kind":"source","signature":"none"}, indent=2)+"\n", encoding="utf-8")
    (root / "release-notes.md").write_text(f"# {version}\n\nSource commit: `{sha}`.\n\nSource archive, not a ready-to-run installer. See README for setup and limitations.\nSHA256SUMS and provenance.json describe this exact archive. No model-quality claim is made.\n\nBefore publishing: inspect the CI result, diff, license and included files; add user-facing changes and known limitations. Restore the previous release to roll back; do not overwrite existing assets.\n", encoding="utf-8")
    return root

if __name__ == "__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--version",required=True)
    p.add_argument("--output",required=True)
    a=p.parse_args()
    print(prepare(a.version,a.output))
