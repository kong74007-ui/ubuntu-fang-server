from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import httpx


DOWNLOAD_ATTEMPTS = 3
RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
logger = logging.getLogger(__name__)


async def download_with_retry(
    url: str,
    output: str | Path,
    *,
    client_factory=httpx.AsyncClient,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Path:
    output_path = Path(output)
    partial_path = output_path.with_suffix(output_path.suffix + ".part")
    host = urlsplit(url).hostname or "unknown-host"
    timeout = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=60.0)
    last_error: BaseException | None = None

    async with client_factory(timeout=timeout) as client:
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                response = await client.get(url)
                response.raise_for_status()
                output_path.parent.mkdir(parents=True, exist_ok=True)
                partial_path.write_bytes(response.content)
                os.replace(partial_path, output_path)
                return output_path
            except asyncio.CancelledError:
                partial_path.unlink(missing_ok=True)
                raise
            except httpx.HTTPStatusError as exc:
                partial_path.unlink(missing_ok=True)
                status = exc.response.status_code
                if status not in RETRYABLE_STATUS_CODES:
                    raise RuntimeError(
                        f"Media download failed from {host}: HTTP {status}"
                    ) from exc
                last_error = exc
            except httpx.TransportError as exc:
                partial_path.unlink(missing_ok=True)
                last_error = exc
            except Exception as exc:
                partial_path.unlink(missing_ok=True)
                raise RuntimeError(
                    f"Media download failed from {host}: {type(exc).__name__}"
                ) from exc

            if attempt < DOWNLOAD_ATTEMPTS:
                logger.warning(
                    "Media download retry host=%s attempt=%s/%s error=%s",
                    host,
                    attempt,
                    DOWNLOAD_ATTEMPTS,
                    type(last_error).__name__,
                )
                await sleep(2 ** attempt)

    error_type = type(last_error).__name__ if last_error else "UnknownError"
    if isinstance(last_error, httpx.HTTPStatusError):
        safe_detail = f"HTTP {last_error.response.status_code}"
    else:
        safe_detail = error_type
    raise RuntimeError(
        f"Media download failed from {host} after {DOWNLOAD_ATTEMPTS} attempts: "
        f"{error_type}: {safe_detail}"
    ) from last_error
