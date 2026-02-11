import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    admin_chat_id: int
    refresh_hours: int

    singbox_path: str
    clash_api_host: str
    clash_api_port: int

    github_token: str | None
    github_repo: str | None
    github_branch: str
    github_output_txt_path: str
    github_output_yaml_path: str

    test_url: str
    test_timeout_ms: int
    max_concurrency: int


def load_settings() -> Settings:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    admin_chat_id_raw = os.environ.get("ADMIN_CHAT_ID", "").strip()
    if not admin_chat_id_raw:
        raise RuntimeError("ADMIN_CHAT_ID is required")

    refresh_hours = int(os.environ.get("REFRESH_HOURS", "4"))

    singbox_path = os.environ.get("SINGBOX_PATH", "sing-box")
    clash_api_host = os.environ.get("CLASH_API_HOST", "127.0.0.1")
    clash_api_port = int(os.environ.get("CLASH_API_PORT", "9090"))

    github_token = os.environ.get("GITHUB_TOKEN")
    github_repo = os.environ.get("GITHUB_REPO")
    github_branch = os.environ.get("GITHUB_BRANCH", "main")
    github_output_txt_path = os.environ.get("GITHUB_OUTPUT_TXT_PATH", "healthy.txt")
    github_output_yaml_path = os.environ.get("GITHUB_OUTPUT_YAML_PATH", "healthy_clash.yaml")

    test_url = os.environ.get("TEST_URL", "https://cp.cloudflare.com/generate_204")
    test_timeout_ms = int(os.environ.get("TEST_TIMEOUT_MS", "6000"))
    max_concurrency = int(os.environ.get("MAX_CONCURRENCY", "20"))

    return Settings(
        telegram_bot_token=token,
        admin_chat_id=int(admin_chat_id_raw),
        refresh_hours=refresh_hours,
        singbox_path=singbox_path,
        clash_api_host=clash_api_host,
        clash_api_port=clash_api_port,
        github_token=github_token.strip() if github_token else None,
        github_repo=github_repo.strip() if github_repo else None,
        github_branch=github_branch,
        github_output_txt_path=github_output_txt_path,
        github_output_yaml_path=github_output_yaml_path,
        test_url=test_url,
        test_timeout_ms=test_timeout_ms,
        max_concurrency=max_concurrency,
    )
