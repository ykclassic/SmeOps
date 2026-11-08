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
