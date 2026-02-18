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

    # === فیلتر قوی برای حذف outboundهای با path نامعتبر ws (مثل %2@) ===
    hex_digits = "0123456789abcdefABCDEF"

    def is_invalid_path(p: str) -> bool:
        if not p or "%" not in p:
            return False
        i = 0
        while i < len(p):
            if p[i] == "%":
                if i + 2 >= len(p) or p[i+1] not in hex_digits or p[i+2] not in hex_digits:
                    return True
                i += 2
            i += 1
        return False

    def has_invalid_ws_path(ob: dict) -> bool:
        def recurse(d) -> bool:
            if isinstance(d, dict):
                transport = d.get("transport")
                if isinstance(transport, dict):
                    t_type = transport.get("type")
                    if t_type in ("ws", "websocket"):
                        path = transport.get("path")
                        if isinstance(path, str) and is_invalid_path(path):
                            return True
                    if recurse(transport):
                        return True
                for v in d.values():
                    if recurse(v):
                        return True
            elif isinstance(d, (list, tuple)):
                for item in d:
                    if recurse(item):
                        return True
            return False
        return recurse(ob)

    bad_indices = []
    for idx, ob in enumerate(outbounds):
        if has_invalid_ws_path(ob):
            tag = ob.get("tag", f"unknown_{idx}")
            print(f"!!! Skipping invalid WS path outbound: {tag} (position {idx})")
            bad_indices.append(idx)

    if bad_indices:
        print(f"Removing {len(bad_indices)} outbounds with invalid WS path escapes")
        outbounds = [ob for idx, ob in enumerate(outbounds) if idx not in bad_indices]

    print(f"After filtering: {len(outbounds)} valid outbounds remaining")
    # === پایان فیلتر ws ===

    # === FIX TYPE: همه فیلدهایی که sing-box باید string باشن ===
    def sanitize_outbound(ob: dict) -> dict:
        if not isinstance(ob, dict):
            return ob

        string_fields = ("password", "uuid", "id", "shortId", "short_id", "alterId", "flow", "method", "host", "path", "server_name")

        for key in string_fields:
            if key in ob:
                val = ob[key]
                if isinstance(val, (int, float, bool)):
                    ob[key] = str(val)
                elif val is None:
                    ob[key] = ""

        # recursive برای transport, tls, reality, mux و ...
        for v in list(ob.values()):
            if isinstance(v, dict):
                sanitize_outbound(v)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        sanitize_outbound(item)
        return ob

    outbounds = [sanitize_outbound(ob.copy()) for ob in outbounds]
    print(f"After type sanitization: {len(outbounds)} outbounds ready for sing-box")
    # === پایان FIX TYPE ===

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


# بقیه توابع (render_outputs و build_commit_message) دقیقاً مثل نسخه اصلی repo هست
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

    by_protocol: dict[str, list[str]] = {}
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
