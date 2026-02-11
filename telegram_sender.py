from __future__ import annotations

import httpx


async def send_message(token: str, chat_id: int, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, data={"chat_id": str(chat_id), "text": text})
        r.raise_for_status()


async def send_document(token: str, chat_id: int, filename: str, content: bytes, caption: str | None = None) -> None:
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    files = {"document": (filename, content)}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, data=data, files=files)
        r.raise_for_status()
