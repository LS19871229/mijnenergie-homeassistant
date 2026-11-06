"""Verbinding met het Mijn Energie-portaal."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=30)


class MijnEnergieError(Exception):
    """Basisfout."""


class CannotConnect(MijnEnergieError):
    """Portaal niet bereikbaar of gaf een onverwacht antwoord."""


class InvalidCode(MijnEnergieError):
    """Koppelcode onbekend, verlopen of al gebruikt."""


class InvalidAuth(MijnEnergieError):
    """Token wordt niet (meer) geaccepteerd."""


class TooManyAttempts(MijnEnergieError):
    """Te veel koppelpogingen vanaf dit adres."""


def normalize_url(url: str) -> str:
    """'eltrading.nl/mijnenergie/' -> 'https://eltrading.nl/mijnenergie'."""
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "https://" + url
    return url


class MijnEnergieApi:
    """Kleine client voor api/pair.php en api/ingest.php."""

    def __init__(self, session: aiohttp.ClientSession, url: str, token: str | None = None) -> None:
        self._session = session
        self._url = normalize_url(url)
        self._token = token

    async def _post(self, path: str, payload: dict[str, Any], auth: bool) -> tuple[int, dict[str, Any]]:
        headers = {"Accept": "application/json"}
        if auth and self._token:
            headers["X-Mijnenergie-Token"] = self._token
        try:
            async with self._session.post(
                f"{self._url}/{path}", json=payload, headers=headers, timeout=TIMEOUT
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    data = None
                if not isinstance(data, dict):
                    # Bijv. een foutpagina van de hosting
                    raise CannotConnect(f"Onverwacht antwoord van {path} (HTTP {resp.status})")
                return resp.status, data
        except (TimeoutError, aiohttp.ClientError) as err:
            raise CannotConnect(str(err) or err.__class__.__name__) from err

    async def pair(self, code: str, integration_version: str, ha_version: str) -> dict[str, Any]:
        """Koppelcode inwisselen. Geeft {'token': ..., 'home': {'id', 'name', 'mode'}}."""
        status, data = await self._post(
            "api/pair.php",
            {"code": code, "integration_version": integration_version, "ha_version": ha_version},
            auth=False,
        )
        if status == 200 and data.get("ok") and data.get("token") and isinstance(data.get("home"), dict):
            return data
        error = data.get("error")
        if status == 429 or error == "too_many_attempts":
            raise TooManyAttempts
        if error == "invalid_code" or status == 400:
            raise InvalidCode
        raise CannotConnect(f"Koppelen mislukt (HTTP {status}, {error})")

    async def send(self, measurements: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
        """Metingen versturen. Geeft {'accepted': n, 'rejected': n}."""
        status, data = await self._post(
            "api/ingest.php", {"measurements": measurements, "meta": meta}, auth=True
        )
        if status == 401:
            raise InvalidAuth
        if status == 200 and data.get("ok"):
            return data
        raise CannotConnect(f"Versturen mislukt (HTTP {status}, {data.get('error')})")
