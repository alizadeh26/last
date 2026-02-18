from __future__ import annotations

import asyncio
from dataclasses import dataclass
import httpx

from singbox_runner import SingBoxRunner


@dataclass(frozen=True)
class CheckHostNode:
    name: str
    country_code: str
    country: str
    city: str


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    line: str

    @property
    def hostport(self) -> str:
        return f"{self.host}:{self.port}"


async def get_nodes(country_code: str = "ir") -> list[CheckHostNode]:
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get("https://check-host.net/nodes/hosts", headers=headers)
        r.raise_for_status()
        data = r.json()

    nodes = data.get("nodes") if isinstance(data, dict) else {}
    out: list[CheckHostNode] = []
    for name, info in nodes.items():
        if not isinstance(info, dict):
            continue
        loc = info.get("location")
        if not isinstance(loc, list) or len(loc) < 3:
            continue
        cc = str(loc[0]).lower()
        if cc != country_code.lower():
            continue
        out.append(CheckHostNode(
            name=name,
            country_code=cc,
            country=str(loc[1]),
            city=str(loc[2])
        ))

    print(f"✅ {len(out)} نود ایرانی از check-host.net پیدا شد")
    return out


async def reachable_from_country(
    endpoints: list[Endpoint],
    country_code: str = "ir",
    max_endpoints: int = 9999,
    concurrency: int = 8,
    poll_wait_seconds: int = 20,
    max_delay_ms: int = 800,
    min_success_nodes: int = 2,
    singbox_path: str | None = None,
    clash_api_host: str = "127.0.0.1",
    clash_api_port: int = 9090,
    test_url: str = "https://cp.cloudflare.com/generate_204",
) -> list[Endpoint]:

    nodes = await get_nodes(country_code)
    node_names = [n.name for n in nodes]
    if not node_names:
        print("⚠️ هیچ نود ایرانی پیدا نشد!")
        return []

    endpoints = list(endpoints)[:max_endpoints]
    sem = asyncio.Semaphore(max(1, concurrency))
    ok: list[Endpoint] = []

    runner = None
    api = None
    if singbox_path:
        try:
            runner = SingBoxRunner(singbox_path, clash_api_host, clash_api_port)
            api = await runner.start([])
            print("🚀 SingBoxRunner برای real delay_test راه‌اندازی شد")
        except Exception as e:
            print(f"⚠️ SingBoxRunner راه‌اندازی نشد: {e} → فقط TCP")

    async def test_one(ep: Endpoint) -> None:
        async with sem:
            rid = await _start_tcp_check(ep, node_names)
            if not rid:
                return
            success_tcp = await _poll_tcp_result(rid, node_names, poll_wait_seconds, min_success_nodes)
            if not success_tcp:
                return

            if api and runner:
                try:
                    delay = await runner.delay_test(api, ep.hostport, test_url, 5000)
                    if delay is None or delay > max_delay_ms or delay <= 0:
                        return
                    print(f"✅ {ep.hostport} → delay={delay}ms (real sing-box)")
                except Exception:
                    pass

            ok.append(ep)
            print(f"🎯 {ep.hostport} از فیلترینگ ایران عبور کرد!")

    await asyncio.gather(*(test_one(ep) for ep in endpoints))

    if runner:
        await runner.close()

    print(f"✅ در نهایت {len(ok)} سرور از ایران کار می‌کنه (real test)")
    return ok


# توابع کمکی TCP
async def _start_tcp_check(endpoint: Endpoint, node_names: list[str]) -> str | None:
    headers = {"Accept": "application/json"}
    params = [("host", endpoint.hostport)] + [("node", n) for n in node_names]
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get("https://check-host.net/check-tcp", headers=headers, params=params)
        if r.status_code != 200:
            return None
        data = r.json()
        return str(data.get("request_id")) if isinstance(data, dict) and data.get("ok") == 1 else None


async def _poll_tcp_result(request_id: str, node_names: list[str], max_wait: int, min_success: int) -> bool:
    headers = {"Accept": "application/json"}
    deadline = asyncio.get_event_loop().time() + max_wait
    async with httpx.AsyncClient(timeout=30) as client:
        while asyncio.get_event_loop().time() < deadline:
            r = await client.get(f"https://check-host.net/check-result/{request_id}", headers=headers)
            if r.status_code != 200:
                await asyncio.sleep(0.5)
                continue
            data = r.json()
            if not isinstance(data, dict):
                await asyncio.sleep(0.5)
                continue

            success_count = 0
            for node in node_names:
                res_list = data.get(node)
                if isinstance(res_list, list):
                    for item in res_list:
                        if isinstance(item, dict) and "time" in item and "error" not in item:
                            success_count += 1
                            break
            if success_count >= min_success:
                return True
            await asyncio.sleep(0.5)
    return False
