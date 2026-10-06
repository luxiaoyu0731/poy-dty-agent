import json

from app import release_identity


def test_release_identity_never_exposes_paths_or_arbitrary_metadata(tmp_path, monkeypatch):
    manifest = tmp_path / "release.json"
    monkeypatch.setattr(release_identity, "RELEASE_FILE", manifest)
    assert release_identity.read_release_identity() == {"status": "unavailable"}
    expected = {"release_id": "fixture", "release_hash": "a" * 16, "git_sha": "b" * 40}
    manifest.write_text(json.dumps({**expected, "rollback_target": "/private/runtime", "secret": "hidden"}))
    assert release_identity.read_release_identity() == {"status": "available", **expected}
    manifest.write_text("not json")
    assert release_identity.read_release_identity() == {"status": "unavailable"}
