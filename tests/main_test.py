from http import HTTPStatus
from json import JSONDecodeError

import httpx
import pytest
from starlette.testclient import TestClient

from main import app
from src.endpoints import _to_playwright_cookies
from src.models import LinkRequest

client = TestClient(app)

test_websites = [
    "https://ext.to/",
    # "https://www.ygg.re/",
    "https://extratorrent.st/",
    "https://speed.cd/login",
    'https://www.yggtorrent.top/engine/search?do=search&order=desc&sort=publish_date&name="UNESCAPED"+"DOUBLEQUOTES"&category=2145',
    "https://1337x.to/home/",
]


@pytest.mark.parametrize("website", test_websites)
def test_bypass(website: str):
    """
    Tests if the service can bypass cloudflare/DDOS-GUARD on given websites.

    This test is skipped if the website is not reachable or does not have cloudflare/DDOS-GUARD.
    """
    test_request = httpx.get(
        website,
    )
    if (
        test_request.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR
        and "Just a moment..." not in test_request.text
    ):
        try:
            error_details = test_request.json()
        except JSONDecodeError:
            error_details = test_request.text
        pytest.skip(
            f"Skipping {website} - ({test_request.status_code}) {error_details}"
        )

    response = client.post(
        "/v1",
        json=LinkRequest.model_construct(url=website, cmd="request.get").model_dump(),
    )

    assert response.status_code == HTTPStatus.OK


def test_health_check():
    """
    Tests the health check endpoint.

    This test ensures that the health check
    endpoint returns HTTPStatus.OK.
    """
    response = client.get("/health")
    assert response.status_code == HTTPStatus.OK


def test_flaresolverr_cookie_payload_is_accepted():
    payload = {
        "cmd": "request.get",
        "url": "https://example.com",
        "cookies": [
            {
                "name": "cf_clearance",
                "value": "abc123",
                "domain": ".example.com",
                "path": "/",
                "expires": 1893456000,
                "httpOnly": True,
                "secure": True,
                "sameSite": "None",
                "session": False,
                "size": 64,
            }
        ],
    }

    request = LinkRequest.model_validate(payload)

    assert request.cookies is not None
    assert request.cookies[0].name == "cf_clearance"
    assert request.cookies[0].http_only is True
    assert request.cookies[0].same_site == "None"


def test_cookie_conversion_matches_playwright_shape():
    request = LinkRequest.model_validate(
        {
            "cmd": "request.get",
            "url": "https://example.com",
            "cookies": [
                {
                    "name": "cookie1",
                    "value": "value1",
                    "domain": ".example.com",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "no_restriction",
                }
            ],
        }
    )

    converted = _to_playwright_cookies(request.cookies or [], request.url)

    assert converted[0]["name"] == "cookie1"
    assert converted[0]["value"] == "value1"
    assert converted[0]["domain"] == ".example.com"
    assert converted[0]["path"] == "/"
    assert converted[0]["httpOnly"] is True
    assert converted[0]["secure"] is True
    assert converted[0]["sameSite"] == "None"


def test_cloudflare_input_cookies_are_not_overwritten():
    request = LinkRequest.model_validate(
        {
            "cmd": "request.get",
            "url": "https://example.com",
            "cookies": [
                {
                    "name": "cf_clearance",
                    "value": "do-not-set",
                    "domain": ".example.com",
                },
                {
                    "name": "custom_cookie",
                    "value": "keep-me",
                    "domain": ".example.com",
                },
            ],
        }
    )

    converted = _to_playwright_cookies(request.cookies or [], request.url)

    assert len(converted) == 1
    assert converted[0]["name"] == "custom_cookie"
