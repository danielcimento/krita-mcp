"""Placeholder live test: the plugin's HTTP bridge answers /health."""

import httpx


def test_health_ok(krita_url):
    response = httpx.get(f"{krita_url}/health", timeout=5.0)
    assert response.status_code == 200
    assert response.json().get("status") == "ok"
