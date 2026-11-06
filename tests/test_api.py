"""De echte client tegen een nagebouwd portaal (zelfde antwoorden als api/pair.php en api/ingest.php)."""

from __future__ import annotations

from typing import Any

import aiohttp
import pytest
from aiohttp import web

from custom_components.mijnenergie.api import (
    CannotConnect,
    InvalidAuth,
    InvalidCode,
    MijnEnergieApi,
    TooManyAttempts,
    normalize_url,
)


@pytest.fixture
async def portal(socket_enabled: None):
    """Klein nep-portaal op localhost."""
    state: dict[str, Any] = {"received": [], "headers": [], "fails": 0}

    async def pair(request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("code") == "429429":
            return web.json_response({"ok": False, "error": "too_many_attempts"}, status=429)
        if body.get("code") != "482913":
            return web.json_response({"ok": False, "error": "invalid_code"}, status=400)
        return web.json_response({"ok": True, "token": "ep_abc", "home": {"id": 12, "name": "Fam. de Vries", "mode": "pv"}})

    async def ingest(request: web.Request) -> web.Response:
        state["headers"].append(dict(request.headers))
        if request.headers.get("X-Mijnenergie-Token") != "ep_abc":
            return web.json_response({"ok": False, "error": "invalid_token"}, status=401)
        body = await request.json()
        state["received"].append(body)
        return web.json_response({"ok": True, "accepted": len(body["measurements"]), "rejected": 0})

    async def broken(request: web.Request) -> web.Response:
        return web.Response(text="<html>503 Service Unavailable</html>", status=503, content_type="text/html")

    app = web.Application()
    app.router.add_post("/mijnenergie/api/pair.php", pair)
    app.router.add_post("/mijnenergie/api/ingest.php", ingest)
    app.router.add_post("/kapot/api/ingest.php", broken)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    state["base"] = f"http://127.0.0.1:{port}"
    yield state
    await runner.cleanup()


def test_normalize_url() -> None:
    assert normalize_url("eltrading.nl/mijnenergie/") == "https://eltrading.nl/mijnenergie"
    assert normalize_url(" https://eltrading.nl/mijnenergie ") == "https://eltrading.nl/mijnenergie"


async def test_pair_and_send(portal: dict[str, Any]) -> None:
    async with aiohttp.ClientSession() as session:
        api = MijnEnergieApi(session, portal["base"] + "/mijnenergie/")
        result = await api.pair("482913", "1.0.0", "2026.11.0")
        assert result["token"] == "ep_abc"
        assert result["home"]["id"] == 12

        with pytest.raises(InvalidCode):
            await api.pair("000000", "1.0.0", "2026.11.0")
        with pytest.raises(TooManyAttempts):
            await api.pair("429429", "1.0.0", "2026.11.0")

        api = MijnEnergieApi(session, portal["base"] + "/mijnenergie", "ep_abc")
        res = await api.send([{"ts": "2026-11-06T13:42:00+01:00"}], {"integration_version": "1.0.0", "queue": 0, "meters": ["p1"]})
        assert res["accepted"] == 1
        assert portal["received"][0]["meta"]["meters"] == ["p1"]
        assert portal["headers"][0]["Content-Type"].startswith("application/json")

        with pytest.raises(InvalidAuth):
            await MijnEnergieApi(session, portal["base"] + "/mijnenergie", "fout").send([], {})


async def test_errors_become_cannot_connect(portal: dict[str, Any]) -> None:
    async with aiohttp.ClientSession() as session:
        # Foutpagina van de hosting (HTML, 503)
        with pytest.raises(CannotConnect):
            await MijnEnergieApi(session, portal["base"] + "/kapot", "ep_abc").send([], {})
        # Niets luistert op deze poort
        with pytest.raises(CannotConnect):
            await MijnEnergieApi(session, "http://127.0.0.1:9", "ep_abc").send([], {})
