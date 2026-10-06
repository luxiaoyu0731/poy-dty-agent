"""Retired runtime providers cannot re-enter collection via stored source IDs."""
import pytest

from app.settings import settings
from app.source_registry import get_source, list_sources


@pytest.mark.parametrize("source_id", ["dce_meg", "ccf_dom_daily", "ccf_average_price", "ccf_manual_export"])
def test_retired_source_has_no_runtime_registration(source_id):
    assert get_source(source_id) is None
    assert source_id not in {source.source_id for source in list_sources(include_soft_removed=True)}
    assert not settings.source_credentials_configured(source_id)


def test_retired_credentials_are_not_runtime_settings():
    assert not hasattr(settings, "dce_api_key")
    assert not hasattr(settings, "dce_secret")
