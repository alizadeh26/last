from __future__ import annotations

import asyncio
import os
import socket
import time
from datetime import datetime

import httpx
import yaml
from check_host import Endpoint, reachable_from_country_tcp
from checker import check_nodes, collect_nodes, render_outputs
from config import load_settings
from speed_test import find_fast_nodes, render_fast_list
from subs import node_from_clash_proxy, node_from_share_link
from telegram_sender import send_document, send_message


def load_subscription_urls(path: str) -> list[str]:
    if not os.path.exists(path):
        raise RuntimeError(f"subscriptions file not found: {path}")
    urls: list[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            u = line.strip()
            if not u:
                continue
            urls.append(u)
    return urls


async def main() -> None:
    settings = load_settings()

    subs_file = os.environ.get("SUBSCRIPTIONS_FILE", "subscriptions.txt")
    urls = load_subscription_urls(subs_file)

    await send_message(settings.telegram_bot_token, settings.admin_chat_id, "شروع بررسی...")

    nodes = await collect_nodes(urls)
    if not nodes:
        await send_message(settings.telegram_bot_token, settings.admin_chat_id, "هیچ نودی استخراج نشد")
        return

    res = await check_nodes(
        singbox_path=settings.singbox_path,
        clash_api_host=settings.clash_api_host,
        clash_api_port=settings.clash_api_port,
        test_url=settings.test_url,
        timeout_ms=settings.test_timeout_ms,
        max_concurrency=settings.max_concurrency,
        nodes=nodes,
    )

    txt_bytes, yml_bytes = render_outputs(res)

    check_host_country = os.environ.get("CHECK_HOST_COUNTRY", "ir").strip().lower()
    check_host_max_endpoints = int(os.environ.get("CHECK_HOST_MAX_ENDPOINTS", "50"))
    check_host_concurrency = int(os.environ.get("CHECK_HOST_CONCURRENCY", "5"))
    check_host_poll_wait_seconds = int(os.environ.get("CHECK_HOST_POLL_WAIT_SECONDS", "15"))
    iran_path = os.environ.get("GITHUB_OUTPUT_IR_PATH", "iran_reachable.txt")

    endpoints: list[Endpoint] = []
    seen_hostport: set[str] = set()

    for link in res.healthy_links:
        try:
            n = node_from_share_link(link)
            host = str(n.outbound.get("server") or "").strip()
            port = int(n.outbound.get("server_port"))
            if host and port:
                ep = Endpoint(host=host, port=port, line=link)
                if ep.hostport not in seen_hostport:
                    seen_hostport.add(ep.hostport)
                    endpoints.append(ep)
        except Exception:
            continue

    for p in res.healthy_clash_proxies:
        try:
            host = str(p.get("server") or "").strip()
            port = int(p.get("port"))
            name = str(p.get("name") or "").strip()
            if host and port:
                line = f"{host}:{port}" + (f"\t{name}" if name else "")
                ep = Endpoint(host=host, port=port, line=line)
                if ep.hostport not in seen_hostport:
                    seen_hostport.add(ep.hostport)
                    endpoints.append(ep)
        except Exception:
            continue

    iran_ok: list[Endpoint] = []
    if endpoints and check_host_country:
        try:
            iran_ok = await reachable_from_country_tcp(
                endpoints,
                country_code=check_host_country,
                max_endpoints=check_host_max_endpoints,
                concurrency=check_host_concurrency,
                poll_wait_seconds=check_host_poll_wait_seconds,
            )
        except Exception:
            iran_ok = []

    iran_bytes = ("\n".join(ep.line for ep in iran_ok).strip() + "\n").encode("utf-8")

    # تعیین کشور هر لینک سالم (healthy.txt) بر اساس IP سرور
    print(f"[DEBUG] شروع گروه‌بندی بر اساس کشور - تعداد لینک‌های سالم: {len(res.healthy_links)}")
    
    def _is_ip(host: str) -> bool:
        try:
            socket.inet_aton(host)
            return True
        except OSError:
            return False

    country_cache: dict[str, str] = {}

    def _lookup_country(host: str) -> str:
        h = host.strip()
        if not h:
            print(f"[DEBUG] Host خالی است")
            return "UNKNOWN"
        
        is_ip_addr = _is_ip(h)
        print(f"[DEBUG] Host: {h} | نوع: {'IP' if is_ip_addr else 'Hostname'}")
        
        try:
            ip = h if is_ip_addr else socket.gethostbyname(h)
            if not is_ip_addr:
                print(f"[DEBUG] Hostname '{h}' به IP تبدیل شد: {ip}")
        except Exception as e:
            print(f"[DEBUG] خطا در تبدیل hostname '{h}' به IP: {e}")
            return "UNKNOWN"

        if ip in country_cache:
            cached_country = country_cache[ip]
            print(f"[DEBUG] IP {ip} از cache: کشور = {cached_country}")
            return cached_country

        # تأخیر برای جلوگیری از 429 (ip-api.com حدود ۴۵ درخواست در دقیقه مجاز دارد)
        time.sleep(2)

        code = "UNKNOWN"
        # اول ip-api.com (۴۵ درخواست/دقیقه)، بعد ipapi.co
        apis = [
            ("http://ip-api.com/json/{ip}?fields=countryCode", "countryCode"),
            ("https://ipapi.co/{ip}/json/", "country_code"),
        ]
        for url_tpl, key in apis:
            try:
                print(f"[DEBUG] در حال lookup GeoIP برای IP: {ip}")
                r = httpx.get(url_tpl.format(ip=ip), timeout=10)
                print(f"[DEBUG] پاسخ GeoIP: status={r.status_code}")
                if r.status_code == 200:
                    data = r.json()
                    c = str(data.get(key) or "").strip().upper()
                    if c:
                        code = c
                        print(f"[DEBUG] کشور پیدا شد: {code}")
                        break
                elif r.status_code == 429:
                    print(f"[DEBUG] محدودیت درخواست (429)، تلاش با API بعدی...")
                    time.sleep(3)
                    continue
            except Exception as e:
                print(f"[DEBUG] خطا در درخواست GeoIP: {e}")
                continue
        else:
            print(f"[DEBUG] کشور پیدا نشد یا UNKNOWN است")

        country_cache[ip] = code
        return code

    links_by_country: dict[str, list[str]] = {}
    processed_count = 0
    skipped_count = 0
    
    for link in res.healthy_links:
        try:
            n = node_from_share_link(link)
            host = str(n.outbound.get("server") or "").strip()
            if not host:
                skipped_count += 1
                print(f"[DEBUG] لینک بدون host نادیده گرفته شد")
                continue
            
            print(f"[DEBUG] پردازش لینک | host: {host}")
            c = _lookup_country(host)
            if not c or c == "UNKNOWN":
                skipped_count += 1
                print(f"[DEBUG] کشور پیدا نشد یا UNKNOWN است")
                continue
            
            links_by_country.setdefault(c, []).append(link)
            processed_count += 1
            print(f"[DEBUG] لینک به کشور {c} اضافه شد")
        except Exception as e:
            skipped_count += 1
            print(f"[DEBUG] خطا در پردازش لینک: {e}")
            continue
    
    print(f"[DEBUG] خلاصه: پردازش شده={processed_count}, نادیده گرفته شده={skipped_count}")
    print(f"[DEBUG] کشورهای پیدا شده: {list(links_by_country.keys())}")
    for country, links in links_by_country.items():
        print(f"[DEBUG]   {country}: {len(links)} لینک")

    speed_enabled = os.environ.get("SPEED_TEST_ENABLED", "1").strip().lower() not in ("0", "false", "no")
    speed_threshold_kib_s = int(os.environ.get("SPEED_TEST_THRESHOLD_KIB_S", "500"))
    speed_max_nodes = int(os.environ.get("SPEED_TEST_MAX_NODES", "10"))
    speed_concurrency = int(os.environ.get("SPEED_TEST_CONCURRENCY", "1"))
    speed_download_bytes = int(os.environ.get("SPEED_TEST_DOWNLOAD_BYTES", "2000000"))
    speed_upload_bytes = int(os.environ.get("SPEED_TEST_UPLOAD_BYTES", "1000000"))
    speed_timeout_seconds = int(os.environ.get("SPEED_TEST_TIMEOUT_SECONDS", "25"))
    fast_path = os.environ.get("GITHUB_OUTPUT_FAST_PATH", "fast_500kbps.txt")

    fast_bytes = b"\n"
    fast_count = 0
    if speed_enabled:
        speed_outbounds: list[dict] = []
        labels_by_tag: dict[str, str] = {}
        seen_tags: set[str] = set()

        for link in res.healthy_links:
            try:
                n = node_from_share_link(link)
            except Exception:
                continue
            tag = str(n.outbound.get("tag") or "")
            if not tag or tag in seen_tags:
                continue
            seen_tags.add(tag)
            speed_outbounds.append(n.outbound)
            labels_by_tag[tag] = link

        for p in res.healthy_clash_proxies:
            try:
                n = node_from_clash_proxy(p)
            except Exception:
                continue
            if not n:
                continue
            tag = str(n.outbound.get("tag") or "")
            if not tag or tag in seen_tags:
                continue
            seen_tags.add(tag)
            speed_outbounds.append(n.outbound)
            labels_by_tag[tag] = str(p.get("name") or tag)

        try:
            fast = await find_fast_nodes(
                singbox_path=settings.singbox_path,
                clash_api_host=settings.clash_api_host,
                clash_api_port=settings.clash_api_port,
                outbounds=speed_outbounds,
                labels_by_tag=labels_by_tag,
                threshold_kib_s=speed_threshold_kib_s,
                max_nodes=speed_max_nodes,
                concurrency=speed_concurrency,
                download_bytes=speed_download_bytes,
                upload_bytes=speed_upload_bytes,
                timeout_seconds=speed_timeout_seconds,
            )
            fast_bytes = render_fast_list(fast)
            fast_count = len(fast)
        except Exception:
            fast_bytes = b"\n"
            fast_count = 0

    txt_path = settings.github_output_txt_path
    yml_path = settings.github_output_yaml_path

    os.makedirs(os.path.dirname(txt_path) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(yml_path) or ".", exist_ok=True)

    with open(txt_path, "wb") as f:
        f.write(txt_bytes)

    with open(yml_path, "wb") as f:
        f.write(yml_bytes)

    # تولید فایل‌های جداگانه بر اساس نوع پروتکل (Clash YAML)
    base_dir = os.path.dirname(yml_path) or "."
    proxies = [p for p in res.healthy_clash_proxies if isinstance(p, dict)]
    by_protocol: dict[str, list[dict]] = {}
    for p in proxies:
        ptype = str(p.get("type") or "").lower() or "unknown"
        by_protocol.setdefault(ptype, []).append(p)

    for ptype, plist in by_protocol.items():
        if not plist or ptype == "unknown":
            continue
        names = [str(p.get("name")) for p in plist if p.get("name")]
        yaml_obj = {
            "port": 7890,
            "socks-port": 7891,
            "allow-lan": True,
            "mode": "Rule",
            "log-level": "silent",
            "proxies": plist,
            "proxy-groups": [
                {
                    "name": "AUTO",
                    "type": "url-test",
                    "url": settings.test_url,
                    "interval": 300,
                    "proxies": names,
                }
            ],
            "rules": ["MATCH,AUTO"],
        }
        yml_proto = yaml.safe_dump(yaml_obj, allow_unicode=True, sort_keys=False).encode("utf-8")
        proto_path = os.path.join(base_dir, f"healthy_{ptype}.yaml")
        with open(proto_path, "wb") as f:
            f.write(yml_proto)

    # تولید فایل‌های متنی جداگانه بر اساس کشور (بر پایه healthy.txt)
    print(f"[DEBUG] شروع ساخت فایل‌های کشوری - تعداد کشورها: {len(links_by_country)}")
    files_created = 0
    for country, links in links_by_country.items():
        if not links:
            print(f"[DEBUG] کشور {country}: لیست خالی است، رد می‌شود")
            continue
        
        print(f"[DEBUG] ساخت فایل برای کشور {country} با {len(links)} لینک")
        country_txt = ("\n".join(links).strip() + "\n").encode("utf-8")
        country_txt_path = os.path.join(base_dir, f"healthy_country_{country}.txt")
        print(f"[DEBUG] مسیر فایل: {country_txt_path}")
        
        try:
            with open(country_txt_path, "wb") as f:
                f.write(country_txt)
            files_created += 1
            print(f"[DEBUG] ✓ فایل {country_txt_path} با موفقیت ساخته شد ({len(country_txt)} بایت)")
        except Exception as e:
            print(f"[DEBUG] ✗ خطا در ساخت فایل {country_txt_path}: {e}")
    
    print(f"[DEBUG] تعداد فایل‌های کشوری ساخته شده: {files_created}")

    os.makedirs(os.path.dirname(iran_path) or ".", exist_ok=True)
    with open(iran_path, "wb") as f:
        f.write(iran_bytes)

    os.makedirs(os.path.dirname(fast_path) or ".", exist_ok=True)
    with open(fast_path, "wb") as f:
        f.write(fast_bytes)

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    await send_document(
        settings.telegram_bot_token,
        settings.admin_chat_id,
        filename=f"healthy_{ts}.txt",
        content=txt_bytes,
        caption=f"Healthy links: {len(res.healthy_links)}",
    )
    await send_document(
        settings.telegram_bot_token,
        settings.admin_chat_id,
        filename=f"healthy_{ts}.yaml",
        content=yml_bytes,
        caption=f"Healthy clash proxies: {len(res.healthy_clash_proxies)}",
    )

    await send_document(
        settings.telegram_bot_token,
        settings.admin_chat_id,
        filename=f"iran_reachable_{ts}.txt",
        content=iran_bytes,
        caption=f"Reachable from {check_host_country.upper()} (TCP): {len(iran_ok)}",
    )

    await send_document(
        settings.telegram_bot_token,
        settings.admin_chat_id,
        filename=f"fast_{ts}.txt",
        content=fast_bytes,
        caption=f"Fast (dl+ul >= {speed_threshold_kib_s} KiB/s): {fast_count}",
    )

    await send_message(
        settings.telegram_bot_token,
        settings.admin_chat_id,
        f"تمام شد. links={len(res.healthy_links)} proxies={len(res.healthy_clash_proxies)} ir={len(iran_ok)} fast={fast_count}",
    )


if __name__ == "__main__":
    asyncio.run(main())
