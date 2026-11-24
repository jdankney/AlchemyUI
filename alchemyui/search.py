"""Phase 9: find a reference picture, and fetch it without trusting it.

The router may ask for a garment or object it has never seen ("me in Nike Pro
sweatpants"). AlchemyUI sends that phrase to SearXNG, offers the image results
to the person, and only after they pick one does it download the pixels. The
download is deliberately hostile-input handling: public hosts only, every
redirect re-checked, a byte cap, and Pillow decodes and re-encodes it before
ComfyUI ever sees it. The server never forwards the remote bytes.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx

from .config import Settings
from .storage import UploadRejected, normalise_upload


class SearchError(Exception):
    """A search or reference fetch failed in a way worth showing the person."""


def _public_address(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


async def _resolve_host(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.run_in_executor(
            None, lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
    except OSError as e:
        raise SearchError(f"Could not resolve {host}: {e}") from e
    return sorted({info[4][0] for info in infos})


async def _check_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SearchError("That reference is not a web link we can fetch.")
    if parsed.username or parsed.password:
        raise SearchError("That reference link contains credentials.")
    host = parsed.hostname
    if not host:
        raise SearchError("That reference link has no host.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not _public_address(host):
            raise SearchError("That reference is on a private address.")
        return
    if host.lower() in {"localhost", "localhost.localdomain"}:
        raise SearchError("That reference is on a private address.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addrs = await _resolve_host(host, port)
    if not addrs or any(not _public_address(a) for a in addrs):
        raise SearchError("That reference resolves to a private address.")


async def image_search(query: str, settings: Settings, *,
                       transport: httpx.AsyncBaseTransport | None = None) -> list[dict]:
    """Search SearXNG's image category. Returns the metadata only; the image
    itself is not downloaded until the person picks one."""
    base = settings.search_url.strip()
    if not base:
        raise SearchError("Web reference search is not configured yet.")
    url = base if urlparse(base).path.rstrip("/").endswith("/search") \
        else base.rstrip("/") + "/search"
    params = {"q": query, "format": "json", "categories": "images", "safesearch": "1"}
    headers = {"User-Agent": settings.search_user_agent}
    try:
        async with httpx.AsyncClient(transport=transport, timeout=settings.search_timeout_s,
                                     headers=headers, follow_redirects=True) as client:
            response = await client.get(url, params=params)
    except (httpx.HTTPError, OSError) as e:
        raise SearchError(f"The image search is unreachable ({type(e).__name__}).") from e
    if response.status_code != 200:
        raise SearchError(f"The image search answered {response.status_code}.")
    try:
        payload = response.json()
    except ValueError as e:
        raise SearchError("The image search returned something that was not JSON.") from e
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise SearchError("The image search returned no result list.")

    out: list[dict] = []
    seen: set[str] = set()
    for r in results:
        if not isinstance(r, dict):
            continue
        image = r.get("img_src") or r.get("thumbnail_src")
        if not isinstance(image, str):
            continue
        image = urljoin(str(response.url), image)
        if urlparse(image).scheme not in ("http", "https") or image in seen:
            continue
        thumb = r.get("thumbnail_src") or image
        thumb = urljoin(str(response.url), thumb)
        seen.add(image)
        out.append({
            "url": image,
            "thumb_url": thumb,
            "title": str(r.get("title") or r.get("source") or "Web image")[:120],
            "source": str(r.get("source") or "")[:80],
            "width": r.get("width"),
            "height": r.get("height"),
        })
        if len(out) >= settings.search_results:
            break
    return out


async def fetch_reference(url: str, settings: Settings, *,
                          transport: httpx.AsyncBaseTransport | None = None) -> tuple[bytes, int, int]:
    """Download one selected image safely, and return a normalised PNG.

    Public addresses only, re-checked at every redirect; a hard byte cap while
    streaming; and Pillow decodes and re-encodes the result. That last step is
    what stops a content-type header from being the only thing standing
    between the network and ComfyUI.
    """
    headers = {"User-Agent": settings.search_user_agent,
               "Accept": "image/*"}
    current = url
    data = b""
    try:
        async with httpx.AsyncClient(transport=transport, timeout=settings.search_timeout_s,
                                     headers=headers, follow_redirects=False) as client:
            for _ in range(settings.search_max_redirects + 1):
                await _check_public_url(current)
                async with client.stream("GET", current) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise SearchError("That reference redirected nowhere.")
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        raise SearchError(
                            f"That reference could not be downloaded ({response.status_code}).")
                    ctype = (response.headers.get("content-type") or "").split(";")[0].lower()
                    if ctype and not ctype.startswith("image/"):
                        raise SearchError("That link is not an image.")
                    chunks: list[bytes] = []
                    total = 0
                    limit = int(settings.search_max_mb * 1024 * 1024)
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > limit:
                            raise SearchError(
                                f"That reference is larger than {settings.search_max_mb:.0f} MB.")
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    break
            else:
                raise SearchError("That reference redirected too many times.")
    except (httpx.HTTPError, OSError) as e:
        raise SearchError(f"That reference could not be downloaded ({type(e).__name__}).") from e

    if not data:
        raise SearchError("That reference was empty.")
    try:
        png, width, height = normalise_upload(
            data, settings.upload_max_edge, settings.search_max_mb)
    except UploadRejected as e:
        raise SearchError(str(e)) from e
    if width < 64 or height < 64:
        raise SearchError("That reference is too small to use.")
    return png, width, height
