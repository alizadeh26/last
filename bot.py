from __future__ import annotations

from datetime import datetime

from telegram import InputFile, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from checker import build_commit_message, check_nodes, collect_nodes, render_outputs
from config import load_settings
from github_uploader import GitHubTarget, GitHubUploader
from storage import Storage


def _is_admin(settings, update: Update) -> bool:
    chat_id = update.effective_chat.id if update.effective_chat else None
    return chat_id == settings.admin_chat_id


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.application.bot_data["settings"]
    if not _is_admin(settings, update):
        return
    await update.message.reply_text(
        "دستورات:\n"
        "/add <url>\n"
        "/list\n"
        "/remove <url>\n"
        "/run"
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.application.bot_data["settings"]
    if not _is_admin(settings, update):
        return
    if not context.args:
        await update.message.reply_text("/add <url>")
        return

    url = context.args[0].strip()
    storage: Storage = context.application.bot_data["storage"]
    ok = await storage.add_subscription(url)
    await update.message.reply_text("اضافه شد" if ok else "قبلا وجود داشت")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.application.bot_data["settings"]
    if not _is_admin(settings, update):
        return
    storage: Storage = context.application.bot_data["storage"]
    urls = await storage.list_subscriptions()
    if not urls:
        await update.message.reply_text("هیچ لینکی ثبت نشده")
        return
    await update.message.reply_text("\n".join(urls))


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.application.bot_data["settings"]
    if not _is_admin(settings, update):
        return
    if not context.args:
        await update.message.reply_text("/remove <url>")
        return
    url = context.args[0].strip()
    storage: Storage = context.application.bot_data["storage"]
    ok = await storage.remove_subscription(url)
    await update.message.reply_text("حذف شد" if ok else "پیدا نشد")


async def _run_check_and_send(app: Application, chat_id: int) -> None:
    settings = app.bot_data["settings"]
    storage: Storage = app.bot_data["storage"]
    urls = await storage.list_subscriptions()
    if not urls:
        await app.bot.send_message(chat_id=chat_id, text="هیچ لینکی ثبت نشده")
        return

    await app.bot.send_message(chat_id=chat_id, text="شروع بررسی...")

    nodes = await collect_nodes(urls)
    if not nodes:
        await app.bot.send_message(chat_id=chat_id, text="هیچ نودی از لینک‌ها استخراج نشد")
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

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    await app.bot.send_document(
        chat_id=chat_id,
        document=InputFile.from_bytes(txt_bytes, filename=f"healthy_{ts}.txt"),
        caption=f"Healthy links: {len(res.healthy_links)}",
    )
    await app.bot.send_document(
        chat_id=chat_id,
        document=InputFile.from_bytes(yml_bytes, filename=f"healthy_{ts}.yaml"),
        caption=f"Healthy clash proxies: {len(res.healthy_clash_proxies)}",
    )

    if settings.github_token and settings.github_repo:
        uploader = GitHubUploader(settings.github_token)
        msg = build_commit_message("Update healthy subscription")
        await uploader.upsert_file(
            GitHubTarget(settings.github_repo, settings.github_branch, settings.github_output_txt_path),
            txt_bytes,
            msg,
        )
        await uploader.upsert_file(
            GitHubTarget(settings.github_repo, settings.github_branch, settings.github_output_yaml_path),
            yml_bytes,
            msg,
        )
        await app.bot.send_message(chat_id=chat_id, text="روی GitHub هم آپدیت شد")


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.application.bot_data["settings"]
    if not _is_admin(settings, update):
        return
    await _run_check_and_send(context.application, settings.admin_chat_id)


async def scheduled_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.application.bot_data["settings"]
    await _run_check_and_send(context.application, settings.admin_chat_id)


async def post_init(app: Application) -> None:
    settings = app.bot_data["settings"]
    storage: Storage = app.bot_data["storage"]
    await storage.init()
    interval_seconds = max(60, int(settings.refresh_hours * 3600))
    app.job_queue.run_repeating(scheduled_job, interval=interval_seconds, first=10)


def main() -> None:
    settings = load_settings()
    storage = Storage()

    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .build()
    )
    app.bot_data["settings"] = settings
    app.bot_data["storage"] = storage

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("run", cmd_run))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
