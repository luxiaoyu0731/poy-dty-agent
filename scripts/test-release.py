"""Test release preparation in a disposable Git repository; no remote mutation."""
import hashlib
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path


class ReleaseTests(unittest.TestCase):
    def test_archive_identity_and_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"repo"; root.mkdir(); (root/"scripts").mkdir()
            script=root/"scripts/prepare-release.py"
            shutil.copyfile(Path(__file__).with_name("prepare-release.py"),script)
            def git(*a): subprocess.run(["git",*a],cwd=root,check=True,capture_output=True)
            git("init"); git("config","user.name","Test"); git("config","user.email","test@example.invalid")
            (root/"README.md").write_text("fixture")
            git("add","README.md","scripts/prepare-release.py"); git("commit","-m","fixture")
            spec=importlib.util.spec_from_file_location("release_fixture",script)
            module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            output=Path(tmp)/"out"; module.prepare("v0.1.0-preview.2",output)
            manifest=json.loads((output/"provenance.json").read_text())
            self.assertEqual(manifest['sha256'],hashlib.sha256((output/manifest['artifact']).read_bytes()).hexdigest())
            with zipfile.ZipFile(output/manifest['artifact']) as z:
                self.assertEqual(z.read('source/README.md'),b'fixture')
            with self.assertRaises(FileExistsError): module.prepare("v0.1.0-preview.2",output)
            with self.assertRaises(ValueError): module.prepare("../../bad",Path(tmp)/"bad")
            (root/"README.md").write_text("dirty")
            with self.assertRaises(ValueError): module.prepare("v0.1.1",Path(tmp)/"dirty")
            self.assertFalse((Path(tmp)/"dirty").exists())

if __name__ == "__main__": unittest.main()
