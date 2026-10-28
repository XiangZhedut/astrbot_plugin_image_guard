"""Bounded media reads. Remote image inspection rejects private-network URLs."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import url2pathname

import aiohttp
from aiohttp.abc import AbstractResolver


class InspectionError(Exception):
    pass


class PublicResolver(AbstractResolver):
    def __init__(self):
        self.delegate = aiohttp.resolver.ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        records = await self.delegate.resolve(host, port, family)
        for record in records:
            if not ipaddress.ip_address(record["host"]).is_global:
                raise InspectionError("remote image resolved to a non-public address")
        return records

    async def close(self):
        await self.delegate.close()


def validate_remote(url: str):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise InspectionError("unsupported image URL")
    if parsed.username or parsed.password:
        raise InspectionError("URLs with credentials are not accepted")
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise InspectionError("private-network image URL")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise InspectionError("private-network image URL")


def local_path(reference: str) -> Path:
    if reference.startswith("file:"):
        parsed = urlsplit(reference)
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            raise InspectionError("network file URIs are not supported")
        value = url2pathname(parsed.path)
        return Path(value).resolve()
    if reference.startswith(("\\\\", "//")):
        raise InspectionError("network file paths are not supported")
    return Path(reference).expanduser().resolve()


class MediaReader:
    def __init__(self, max_bytes: int = 16 * 1024 * 1024, timeout: float = 10):
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.session = None

    async def read(self, reference: str) -> bytes:
        if not isinstance(reference, str) or not reference:
            raise InspectionError("empty media reference")
        if reference.startswith("base64://"):
            encoded = reference[9:]
        elif reference.startswith("data:"):
            header, separator, encoded = reference.partition(",")
            if not separator or ";base64" not in header:
                raise InspectionError("unsupported data URI")
        else:
            encoded = None
        if encoded is not None:
            if len(encoded) > (self.max_bytes + 2) // 3 * 4 + 8:
                raise InspectionError("image exceeds size limit")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise InspectionError("invalid base64 image") from exc
        elif reference.startswith(("http://", "https://")):
            data = await self._download(reference)
        else:
            path = local_path(reference)

            def read_file():
                with path.open("rb") as source:
                    return source.read(self.max_bytes + 1)

            data = await asyncio.to_thread(read_file)
        if not data or len(data) > self.max_bytes:
            raise InspectionError("empty image or image exceeds size limit")
        return data

    async def _download(self, url: str) -> bytes:
        if self.session is None:
            self.session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False),
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                trust_env=False,
            )
        for _ in range(4):
            validate_remote(url)
            async with self.session.get(url, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    url = urljoin(url, response.headers.get("Location", ""))
                    continue
                response.raise_for_status()
                if response.content_length and response.content_length > self.max_bytes:
                    raise InspectionError("remote image exceeds size limit")
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise InspectionError("remote image exceeds size limit")
                    chunks.append(chunk)
                return b"".join(chunks)
        raise InspectionError("too many image redirects")

    async def close(self):
        if self.session:
            await self.session.close()
