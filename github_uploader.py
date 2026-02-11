from __future__ import annotations

import base64
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class GitHubTarget:
    repo: str  # owner/repo
    branch: str
    path: str


class GitHubUploader:
    def __init__(self, token: str) -> None:
        self._token = token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def upsert_file(
        self,
        target: GitHubTarget,
        content_bytes: bytes,
        commit_message: str,
    ) -> None:
        api = f"https://api.github.com/repos/{target.repo}/contents/{target.path}"
        async with httpx.AsyncClient(timeout=30) as client:
            sha: str | None = None
            r = await client.get(
                api,
                headers=self._headers(),
                params={"ref": target.branch},
            )
            if r.status_code == 200:
                sha = r.json().get("sha")
            elif r.status_code not in (404,):
                r.raise_for_status()

            payload = {
                "message": commit_message,
                "content": base64.b64encode(content_bytes).decode("ascii"),
                "branch": target.branch,
            }
            if sha:
                payload["sha"] = sha

            pr = await client.put(api, headers=self._headers(), json=payload)
            pr.raise_for_status()
