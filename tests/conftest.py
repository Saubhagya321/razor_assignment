from wc_connector.config import Config


def make_config(**overrides) -> Config:
    defaults = dict(
        base_url="https://example.test",
        consumer_key="ck_test",
        consumer_secret="cs_test",
        ca_bundle=None,
        page_size=20,
        page_size_max=50,
        max_retries=2,
        timeout_seconds=1,
        max_total_wait_seconds=5,
        max_concurrent_requests=2,
        min_request_interval_ms=0,
        allow_insecure_localhost=True,
    )
    defaults.update(overrides)
    return Config(**defaults)
