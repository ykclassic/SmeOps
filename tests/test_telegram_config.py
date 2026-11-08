import pytest

from app.core.config import Settings


def test_public_api_base_url_accepts_https_origin_and_normalizes_slash():
    settings = Settings(public_api_base_url="https://api.example.com/")

    assert settings.public_api_base_url == "https://api.example.com"


@pytest.mark.parametrize(
    "value",
    [
        "http://api.example.com",
        "https://user:password@api.example.com",
        "https://api.example.com/api",
        "https://api.example.com?host=attacker.example",
        "https://api.example.com#fragment",
    ],
)
def test_public_api_base_url_rejects_non_origin_urls(value):
    with pytest.raises(ValueError):
        Settings(public_api_base_url=value)


def test_staging_requires_explicit_stable_app_secret_key(monkeypatch):
    # CI provides APP_SECRET_KEY globally for the test suite. Remove it here
    # so this test exercises the missing-configuration case deterministically.
    monkeypatch.delenv("APP_SECRET_KEY", raising=False)

    with pytest.raises(ValueError, match="APP_SECRET_KEY"):
        Settings(
            app_env="staging",
            public_api_base_url="https://api.example.com",
            _env_file=None,
        )


def test_staging_rejects_local_default_app_secret_key():
    with pytest.raises(ValueError, match="local default"):
        Settings(
            app_env="staging",
            app_secret_key="local-only-insecure-secret-change-before-deployment",
            public_api_base_url="https://api.example.com",
        )


def test_staging_accepts_explicit_stable_app_secret_key():
    settings = Settings(
        app_env="staging",
        app_secret_key="a-stable-staging-secret-with-more-than-32-chars",
        public_api_base_url="https://api.example.com",
    )

    assert settings.app_env == "staging"
