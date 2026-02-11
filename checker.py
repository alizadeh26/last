from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import socket

import yaml
import httpx

from singbox_runner import SingBoxRunner
from subs import Node, fetch_text, node_from_clash_proxy, node_from_share_link, parse_subscription_payload


@dataclass(frozen=True)
class CheckResult:
    healthy_links: list[str]
    healthy_clash_proxies: list[dict]


async def collect_nodes(urls: list[str]) -> list[Node]:
    nodes: list[Node] = []
    seen_tags: set[str] = set()

    for url in urls:
        try:
            text = await fetch_text(url)
        except Exception:
            continue

        links, proxies = parse_subscription_payload(text)

        for link in links:
            try:
                n = node_from_share_link(link)
            except Exception:
                continue
            if n.tag in seen_tags:
                continue
            seen_tags.add(n.tag)
            nodes.append(n)

        for p in proxies:
            n = node_from_clash_proxy(p)
            if not n:
                continue
            if n.tag in seen_tags:
                continue
            seen_tags.add(n.tag)
            nodes.append(n)

    return nodes


async def check_nodes(
    singbox_path: str,
    clash_api_host: str,
    clash_api_port: int,
    test_url: str,
    timeout_ms: int,
    max_concurrency: int,
    nodes: list[Node],
) -> CheckResult:
    outbounds = [n.outbound for n in nodes]
    sem = asyncio.Semaphore(max_concurrency)

    healthy_links: list[str] = []
    healthy_clash: list[dict] = []

    async with SingBoxRunner(singbox_path, clash_api_host, clash_api_port) as runner:
        api = await runner.start(outbounds)

        async def one(n: Node) -> None:
            async with sem:
                try:
                    d = await runner.delay_test(api, n.tag, test_url, timeout_ms)
                except Exception:
                    return
                if d is None:
                    return
                if n.export_link:
                    healthy_links.append(n.export_link)
                if n.export_clash_proxy:
                    healthy_clash.append(n.export_clash_proxy)

        await asyncio.gather(*(one(n) for n in nodes))

    return CheckResult(healthy_links=healthy_links, healthy_clash_proxies=healthy_clash)


def render_outputs(res: CheckResult) -> tuple[bytes, bytes]:
    txt = "\n".join(res.healthy_links).strip() + "\n"

    proxies = [p for p in res.healthy_clash_proxies if isinstance(p, dict)]

    def _proxy_name(p: dict) -> str | None:
        name = p.get("name")
        return str(name) if name else None

    def _proxy_type(p: dict) -> str:
        return str(p.get("type") or "").lower() or "unknown"

    def _is_ip(host: str) -> bool:
        try:
            socket.inet_aton(host)
            return True
        except OSError:
            return False

    continent_cache: dict[str, str] = {}

    def _lookup_continent(server: str | None) -> str:
        if not server:
            return "UNKNOWN"
        host = server.strip()
        if not host:
            return "UNKNOWN"
        try:
            ip = host if _is_ip(host) else socket.gethostbyname(host)
        except Exception:
            return "UNKNOWN"

        if ip in continent_cache:
            return continent_cache[ip]

        continent = "UNKNOWN"
        try:
            # از یک API عمومی برای GeoIP استفاده می‌کنیم
            r = httpx.get(f"https://ipapi.co/{ip}/json/", timeout=5)
            if r.status_code == 200:
                data = r.json()
                code = str(data.get("continent_code") or "").upper()
                if code:
                    continent = code
        except Exception:
            continent = "UNKNOWN"

        continent_cache[ip] = continent
        return continent

    # گروه‌بندی بر اساس نوع پروتکل
    by_protocol: dict[str, list[str]] = {}
    # گروه‌بندی بر اساس قاره
    by_continent: dict[str, list[str]] = {}

    all_names: list[str] = []
    for p in proxies:
        name = _proxy_name(p)
        if not name:
            continue
        all_names.append(name)

        ptype = _proxy_type(p)
        by_protocol.setdefault(ptype, []).append(name)

        server = p.get("server")
        continent = _lookup_continent(str(server) if server else None)
        by_continent.setdefault(continent, []).append(name)

    proxy_groups: list[dict] = []

    # گروه اصلی AUTO شامل همه پروکسی‌ها
    if all_names:
        proxy_groups.append(
            {
                "name": "AUTO",
                "type": "url-test",
                "url": "https://cp.cloudflare.com/generate_204",
                "interval": 300,
                "proxies": all_names,
            }
        )

    # گروه‌های بر اساس نوع پروتکل
    for ptype, names in sorted(by_protocol.items()):
        if not names:
            continue
        group_name = f"PROTO-{ptype.upper()}"
        proxy_groups.append(
            {
                "name": group_name,
                "type": "url-test",
                "url": "https://cp.cloudflare.com/generate_204",
                "interval": 300,
                "proxies": names,
            }
        )

    # گروه‌های بر اساس قاره
    for continent, names in sorted(by_continent.items()):
        if not names or continent == "UNKNOWN":
            continue
        group_name = f"REGION-{continent}"
        proxy_groups.append(
            {
                "name": group_name,
                "type": "url-test",
                "url": "https://cp.cloudflare.com/generate_204",
                "interval": 300,
                "proxies": names,
            }
        )

    yaml_obj = {
        "port": 7890,
        "socks-port": 7891,
        "allow-lan": True,
        "mode": "Rule",
        "log-level": "silent",
        "proxies": proxies,
        "proxy-groups": proxy_groups,
        "rules": ["MATCH,AUTO"],
    }
    yml = yaml.safe_dump(yaml_obj, allow_unicode=True, sort_keys=False).encode("utf-8")
    return txt.encode("utf-8"), yml


def build_commit_message(prefix: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    return f"{prefix} {ts}"
