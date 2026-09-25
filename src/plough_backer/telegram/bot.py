"""python-telegram-bot wiring for AdminCommands (thin: all logic is in handlers.py)."""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from plough_backer.enums import TradingStatus
from plough_backer.telegram.handlers import AdminCommands, Reply


def _markup(reply: Reply) -> InlineKeyboardMarkup | None:
    if not reply.buttons:
        return None
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=data) for label, data in row]
            for row in reply.buttons
        ]
    )


def build_application(token: str, commands: AdminCommands) -> Application:  # type: ignore[type-arg]
    app = Application.builder().token(token).build()

    def command(
        fn: Callable[[int | None, list[str]], Reply],
    ) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]]:
        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if update.effective_message is None:
                return
            user = update.effective_user.id if update.effective_user else None
            # DB/MT5 calls are blocking: keep them off the event loop.
            reply = await asyncio.to_thread(fn, user, list(context.args or []))
            await update.effective_message.reply_text(reply.text, reply_markup=_markup(reply))

        return handler

    async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()
        user = query.from_user.id if query.from_user else None
        reply = await asyncio.to_thread(commands.callback, user, query.data)
        await query.edit_message_text(reply.text, reply_markup=_markup(reply))

    routes: dict[str, Callable[[int | None, list[str]], Reply]] = {
        "start": lambda u, a: commands.dashboard(u),
        "dashboard": lambda u, a: commands.dashboard(u),
        "status": lambda u, a: commands.status(u),
        "mode": lambda u, a: commands.mode_menu(u),
        "lock": lambda u, a: commands.lock_ask(u, a[0]) if a else commands.lock_menu(u),
        "pause": lambda u, a: commands.set_status(u, TradingStatus.PAUSED),
        "resume": lambda u, a: commands.set_status(u, TradingStatus.RUNNING),
        "stop": lambda u, a: commands.set_status(u, TradingStatus.STOPPED),
    }
    for name, fn in routes.items():
        app.add_handler(CommandHandler(name, command(fn)))
    app.add_handler(CallbackQueryHandler(on_callback))
    return app
