from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from singbox_runner import ClashApiConfig, SingBoxRunner


@dataclass(frozen=True)
class SpeedResult:
    tag: str
    label: str
    download_bps: float
    upload_bps: float

    @property
    def download_kib_s(self) -> float:
        return self.download_bps / 1024.0

    @property
    def upload_kib_s(self) -> float:
        return self.upload_bps / 1024.0


async def _stream_download(client: httpx.AsyncClient, url: str) -> tuple[int, float]:
    start = time.perf_counter()
    total = 0
    async with client.stream("GET", url) as r:
        r.raise_for_status()
        async for chunk in r.aiter_bytes():
            total += len(chunk)
    elapsed = max(0.001, time.perf_counter() - start)
    return total, elapsed


async def _upload(client: httpx.AsyncClient, url: str, size: int) -> tuple[int, float]:
    payload = b"0" * size
    start = time.perf_counter()
    r = await client.post(url, content=payload, headers={"Content-Type": "application/octet-stream"})
    r.raise_for_status()
    elapsed = max(0.001, time.perf_counter() - start)
    return size, elapsed


def _format_speed_line(res: SpeedResult) -> str:
    return (
        f"{res.label}\t"
        f"dl_kib_s={res.download_kib_s:.0f}\t"
        f"ul_kib_s={res.upload_kib_s:.0f}"
    )


async def find_fast_nodes(
    singbox_path: str,
    clash_api_host: str,
    clash_api_port: int,
    outbounds: list[dict],
    labels_by_tag: dict[str, str],
    threshold_kib_s: int = 500,
    max_nodes: int = 20,
    concurrency: int = 1,
    download_bytes: int = 2_000_000,
    upload_bytes: int = 1_000_000,
    timeout_seconds: int = 25,
    download_base: str = "https://speed.cloudflare.com/__down",
    upload_base: str = "https://speed.cloudflare.com/__up",
    selector_tag: str = "PROXY",
 ) -> list[SpeedResult]:
    max_nodes = max(0, int(max_nodes))
    threshold_bps = float(threshold_kib_s) * 1024.0
    outbounds = [o for o in outbounds if isinstance(o, dict) and o.get("tag")]
    outbounds = outbounds[:max_nodes]

    sem = asyncio.Semaphore(max(1, int(concurrency)))
    ok: list[SpeedResult] = []

    async with SingBoxRunner(singbox_path, clash_api_host, clash_api_port) as runner:
        api = await runner.start(outbounds, enable_selector=True, selector_tag=selector_tag)
        proxy_url = f"http://127.0.0.1:10809"

        async def one(ob: dict) -> None:
            async with sem:
                tag = str(ob.get("tag"))
                label = labels_by_tag.get(tag, tag)
                try:
                    await runner.select_outbound(api, selector_tag, tag)
                except Exception:
                    return

                await asyncio.sleep(0.15)

                dl_url = f"{download_base}?bytes={int(download_bytes)}"
                ul_url = f"{upload_base}?bytes={int(upload_bytes)}"

                try:
                    timeout = httpx.Timeout(timeout_seconds)
                    async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout, follow_redirects=True) as client:
                        dl_bytes, dl_elapsed = await _stream_download(client, dl_url)
                        ul_bytes, ul_elapsed = await _upload(client, ul_url, int(upload_bytes))
                except Exception:
                    return

                dl_bps = float(dl_bytes) / max(0.001, dl_elapsed)
                ul_bps = float(ul_bytes) / max(0.001, ul_elapsed)

                if dl_bps >= threshold_bps and ul_bps >= threshold_bps:
                    ok.append(
                        SpeedResult(
                            tag=tag,
                            label=label,
                            download_bps=dl_bps,
                            upload_bps=ul_bps,
                        )
                    )

        await asyncio.gather(*(one(ob) for ob in outbounds))

    ok.sort(key=lambda r: (r.download_bps + r.upload_bps), reverse=True)
    return ok


def render_fast_list(results: list[SpeedResult]) -> bytes:
    text = "\n".join(_format_speed_line(r) for r in results).strip() + "\n"
    return text.encode("utf-8")
