"""SPEC.md section 9 test 12: config refuses http:// for non-localhost hosts."""

from __future__ import annotations

import pytest

from wc_connector.config import ConfigError, load_config


def _env(**overrides) -> dict:
    base = {
        "WC_BASE_URL": "https://shop.example.com",
        "WC_CONSUMER_KEY": "ck_test",
        "WC_CONSUMER_SECRET": "cs_test",
    }
    base.update(overrides)
    return base


def test_refuses_http_for_non_localhost_host():
    with pytest.raises(ConfigError, match="plaintext HTTP"):
        load_config(_env(WC_BASE_URL="http://shop.example.com"))


def test_allows_http_for_localhost_by_default():
    config = load_config(_env(WC_BASE_URL="http://localhost:8000"))
    assert config.base_url == "http://localhost:8000"


def test_refuses_http_localhost_when_insecure_flag_disabled():
    with pytest.raises(ConfigError, match="plaintext HTTP"):
        load_config(
            _env(WC_BASE_URL="http://localhost:8000", WC_ALLOW_INSECURE_LOCALHOST="false")
        )


def test_allows_https_for_non_localhost():
    config = load_config(_env())
    assert config.base_url == "https://shop.example.com"


def test_missing_required_fields_raises_config_error():
    with pytest.raises(ConfigError, match="WC_CONSUMER_SECRET"):
        load_config({"WC_BASE_URL": "https://shop.example.com", "WC_CONSUMER_KEY": "ck_test"})
