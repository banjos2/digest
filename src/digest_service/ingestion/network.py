import ipaddress
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.resolver import DefaultResolver
from django.conf import settings


class UnsafeSourceURL(ValueError):
    pass


class SourceHTTPError(RuntimeError):
    def __init__(self, code, *, status=None):
        super().__init__(code)
        self.code = code
        self.status = status


def validate_source_url(url):
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise UnsafeSourceURL("invalid_url") from error
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeSourceURL("unsupported_scheme")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise UnsafeSourceURL("invalid_authority")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise UnsafeSourceURL("non_public_hostname")
    if port is not None and port not in {80, 443}:
        raise UnsafeSourceURL("unsupported_port")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return parsed
    if not address.is_global:
        raise UnsafeSourceURL("non_public_address")
    return parsed


def validate_resolved_addresses(addresses):
    if not addresses:
        raise UnsafeSourceURL("dns_no_results")
    for raw_address in addresses:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as error:
            raise UnsafeSourceURL("dns_invalid_result") from error
        if not address.is_global:
            raise UnsafeSourceURL("dns_non_public_address")


class SafeResolver(DefaultResolver):
    async def resolve(self, host, port=0, family=0):
        results = await super().resolve(host, port, family)
        validate_resolved_addresses([result["host"] for result in results])
        return results


@dataclass(frozen=True)
class HTTPDocument:
    url: str
    status: int
    headers: dict
    body: bytes
    content_type: str


class SafeHTTPClient:
    allowed_content_types = {
        "application/atom+xml",
        "application/rss+xml",
        "application/xhtml+xml",
        "application/xml",
        "text/html",
        "text/plain",
        "text/xml",
    }

    def __init__(self):
        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=settings.SOURCE_HTTP_CONNECT_TIMEOUT_SECONDS,
            sock_read=settings.SOURCE_HTTP_READ_TIMEOUT_SECONDS,
        )
        connector = aiohttp.TCPConnector(resolver=SafeResolver(), ttl_dns_cache=0)
        self.session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            cookie_jar=aiohttp.DummyCookieJar(),
            headers={"User-Agent": settings.SOURCE_HTTP_USER_AGENT},
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.close()

    async def close(self):
        await self.session.close()

    async def fetch(self, url, *, headers=None):
        current_url = url
        for redirect_number in range(settings.SOURCE_HTTP_MAX_REDIRECTS + 1):
            validate_source_url(current_url)
            try:
                async with self.session.get(
                    current_url, headers=headers, allow_redirects=False
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        if redirect_number >= settings.SOURCE_HTTP_MAX_REDIRECTS:
                            raise SourceHTTPError("too_many_redirects", status=response.status)
                        location = response.headers.get("Location")
                        if not location:
                            raise SourceHTTPError(
                                "redirect_without_location", status=response.status
                            )
                        current_url = urljoin(str(response.url), location)
                        continue
                    if response.status == 304:
                        return HTTPDocument(str(response.url), 304, dict(response.headers), b"", "")
                    if response.status < 200 or response.status >= 300:
                        raise SourceHTTPError("unexpected_http_status", status=response.status)
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                    if content_type not in self.allowed_content_types:
                        raise SourceHTTPError("unsupported_content_type", status=response.status)
                    chunks = []
                    size = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        size += len(chunk)
                        if size > settings.SOURCE_HTTP_MAX_BYTES:
                            raise SourceHTTPError("response_too_large", status=response.status)
                        chunks.append(chunk)
                    return HTTPDocument(
                        str(response.url),
                        response.status,
                        dict(response.headers),
                        b"".join(chunks),
                        content_type,
                    )
            except (aiohttp.ClientError, TimeoutError) as error:
                raise SourceHTTPError("network_error") from error
        raise SourceHTTPError("too_many_redirects")
