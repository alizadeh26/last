from __future__ import annotations

import asyncio
import os
from datetime import datetime

import httpx
import yaml
from check_host import Endpoint, reachable_from_country   # ← نسخه بهبودیافته
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

    await send_message(settings.telegram_bot_token, settings.admin_chat_id, "🚀 شروع بررسی سرورها...")

    nodes = await collect_nodes(urls)
    if not nodes:
        await send_message(settings.telegram_bot_token, settings.admin_chat_id, "❌ هیچ نودی استخراج نشد")
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

    # ==================== تست واقعی از ایران (real delay_test) ====================
    check_host_country = os.environ.get("CHECK_HOST_COUNTRY", "ir").strip().lower()
    check_host_max_endpoints = int(os.environ.get("CHECK_HOST_MAX_ENDPOINTS", "9999"))
    check_host_concurrency = int(os.environ.get("CHECK_HOST_CONCURRENCY", "8"))
    check_host_poll_wait_seconds = int(os.environ.get("CHECK_HOST_POLL_WAIT_SECONDS", "20"))
    iran_path = os.environ.get("GITHUB_OUTPUT_IR_PATH", "iran_reachable.txt")

    endpoints: list[Endpoint] = []
    seen_hostport: set[str] = set()

    for link in res.healthy_links:
        try:
            n = node_from_share_link(link)
            host = str(n.outbound.get("server") or "").strip()
            port = int(n.outbound.get("server_port") or 0)
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
            port = int(p.get("port") or 0)
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
            print(f"🌍 شروع تست واقعی از ایران ({len(endpoints)} سرور)...")
            iran_ok = await reachable_from_country(
                endpoints,
                country_code=check_host_country,
                max_endpoints=check_host_max_endpoints,
                concurrency=check_host_concurrency,
                poll_wait_seconds=check_host_poll_wait_seconds,
                max_delay_ms=800,
                min_success_nodes=2,
                singbox_path=settings.singbox_path,
                clash_api_host=settings.clash_api_host,
                clash_api_port=settings.clash_api_port,
                test_url=settings.test_url,
            )
        except Exception as e:
            print(f"⚠️ خطا در تست ایران: {e}")
            iran_ok = []

    iran_bytes = ("\n".join(ep.line for ep in iran_ok).strip() + "\n").encode("utf-8")
    # ============================================================================

    # بقیه کد اصلی (سرعت، فایل‌ها، تلگرام و ...)
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
        try:
            fast = await find_fast_nodes(...)  # همان کد قبلی سرعت تست
            fast_bytes = render_fast_list(fast)
            fast_count = len(fast)
        except Exception:
            pass

    txt_path = settings.github_output_txt_path
    yml_path = settings.github_output_yaml_path

    os.makedirs(os.path.dirname(txt_path) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(yml_path) or ".", exist_ok=True)

    with open(txt_path, "wb") as f: f.write(txt_bytes)
    with open(yml_path, "wb") as f: f.write(yml_bytes)
    with open(iran_path, "wb") as f: f.write(iran_bytes)
    with open(fast_path, "wb") as f: f.write(fast_bytes)

    await send_document(settings.telegram_bot_token, settings.admin_chat_id, txt_path, "healthy.txt")
    await send_document(settings.telegram_bot_token, settings.admin_chat_id, yml_path, "healthy_clash.yaml")
    await send_document(settings.telegram_bot_token, settings.admin_chat_id, iran_path, "iran_reachable.txt")
    await send_document(settings.telegram_bot_token, settings.admin_chat_id, fast_path, "fast_500kbps.txt")

    await send_message(
        settings.telegram_bot_token,
        settings.admin_chat_id,
        f"✅ بررسی تمام شد!\n"
        f"سالم: {len(res.healthy_links)}\n"
        f"از ایران: {len(iran_ok)}\n"
        f"سریع: {fast_count}"
    )

    print("🎉 run_once.py با موفقیت تمام شد")


if __name__ == "__main__":
    asyncio.run(main())
