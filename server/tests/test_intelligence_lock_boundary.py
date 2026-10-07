from pathlib import Path

import pytest

from app.industrial_intelligence import service


def test_arbitrary_lock_key_stays_inside_run_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "run_root", lambda: tmp_path)
    key = "../../outside:daily"
    with service.single_flight(key):
        files = list(tmp_path.iterdir())
        assert len(files) == 1 and files[0].suffix == ".lock"
        assert len(files[0].stem) == 64
        with pytest.raises(service.IntelligenceRunError), service.single_flight(key):
            pytest.fail("duplicate lock acquired")
    with service.single_flight(key):
        pass
