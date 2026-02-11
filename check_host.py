from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx


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


async def get_nodes(country_code: str) -> list[CheckHostNode]:
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get("https://check-host.net/nodes/hosts", headers=headers)
        r.raise_for_status()
        data = r.json()

    nodes = data.get("nodes") if isinstance(data, dict) else None
    if not isinstance(nodes, dict):
        return []

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
        out.append(CheckHostNode(name=name, country_code=cc, country=str(loc[1]), city=str(loc[2])))

    return out


async def _start_tcp_check(endpoint: Endpoint, node_names: list[str]) -> str | None:
    headers = {"Accept": "application/json"}
    params: list[tuple[str, str]] = [("host", endpoint.hostport)]
    for n in node_names:
        params.append(("node", n))

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get("https://check-host.net/check-tcp", headers=headers, params=params)
        if r.status_code != 200:
            return None
        data = r.json()

    if not isinstance(data, dict) or data.get("ok") != 1:
        return None
    rid = data.get("request_id")
    return str(rid) if rid else None


def _is_success(item: object) -> bool:
    return isinstance(item, dict) and ("time" in item) and ("error" not in item)


async def _poll_result(request_id: str, node_names: list[str], max_wait_seconds: int) -> bool:
    headers = {"Accept": "application/json"}
    deadline = asyncio.get_event_loop().time() + max_wait_seconds

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

            any_success = False
            all_done = True
            for node in node_names:
                node_res = data.get(node)
                if node_res is None:
                    all_done = False
                    continue
                if isinstance(node_res, list) and any(_is_success(it) for it in node_res):
                    any_success = True
                    break

            if any_success:
                return True
            if all_done:
                return False

            await asyncio.sleep(0.5)

    return False


async def reachable_from_country_tcp(
    endpoints: list[Endpoint],
    country_code: str = "ir",
    max_endpoints: int = 50,
    concurrency: int = 5,
    poll_wait_seconds: int = 15,
) -> list[Endpoint]:
    nodes = await get_nodes(country_code)
    node_names = [n.name for n in nodes]
    if not node_names:
        return []

    endpoints = endpoints[: max(0, int(max_endpoints))]

    sem = asyncio.Semaphore(max(1, int(concurrency)))
    ok: list[Endpoint] = []

    async def one(ep: Endpoint) -> None:
        async with sem:
            rid = await _start_tcp_check(ep, node_names)
            if not rid:
                return
            try:
                success = await _poll_result(rid, node_names, poll_wait_seconds)
            except Exception:
                return
            if success:
                ok.append(ep)

    await asyncio.gather(*(one(ep) for ep in endpoints))
    return ok
