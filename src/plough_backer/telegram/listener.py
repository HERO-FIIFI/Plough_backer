"""Telethon channel listener (README §3, §25, §58). Thin: hands messages to the orchestrator.

Uses a Telegram *user* session (API id/hash) because bots cannot read other people's
channels. Missed-message catch-up after downtime (§58) is Phase 11; dedup already makes
replays safe (INV-01).
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

from telethon import TelegramClient, events

from plough_backer.config import SourcesConfig
from plough_backer.trading.executor import EditOutcome, MultiAccountOutcome, Outcome

OnOutcome = Callable[[str, Outcome | MultiAccountOutcome], Awaitable[None]]
OnEdit = Callable[[EditOutcome], Awaitable[None]]


class MessageOrchestrator(Protocol):
    def process_message(
        self,
        *,
        source_id: str,
        message_id: int,
        message_time: datetime,
        text: str,
        received_at: datetime,
    ) -> Outcome | MultiAccountOutcome: ...

    def process_edit(
        self, *, source_id: str, message_id: int, text: str, edited_at: datetime
    ) -> EditOutcome | None: ...


def chat_map(sources: SourcesConfig) -> dict[int, str]:
    return {s.telegram_chat_id: s.id for s in sources.sources if s.enabled}


async def warm_entities(client: TelegramClient, sources: SourcesConfig) -> list[int]:
    """Cache every joined chat; return configured chat ids the account can't see.

    A fresh session knows no access hashes, so channel updates and catch-up
    (iter_messages) for those chats fail or never arrive, without an error.
    """
    await client.get_dialogs()
    missing = []
    for chat_id in chat_map(sources):
        try:
            await client.get_entity(chat_id)
        except ValueError:  # Telethon: entity not found (account hasn't joined)
            missing.append(chat_id)
    return missing


def build_listener(
    *,
    api_id: int,
    api_hash: str,
    session_path: str,
    sources: SourcesConfig,
    orchestrator: MessageOrchestrator,
    on_outcome: OnOutcome,
    on_edit: OnEdit,
) -> TelegramClient:
    client = TelegramClient(session_path, api_id, api_hash)
    chats = chat_map(sources)

    @client.on(events.NewMessage(chats=list(chats)))  # type: ignore[untyped-decorator]
    async def _new(event: events.NewMessage.Event) -> None:
        source_id = chats[event.chat_id]
        outcome = await asyncio.to_thread(
            orchestrator.process_message,
            source_id=source_id,
            message_id=event.message.id,
            message_time=event.message.date,
            text=event.raw_text or "",
            received_at=datetime.now(UTC),
        )
        await on_outcome(source_id, outcome)

    @client.on(events.MessageEdited(chats=list(chats)))  # type: ignore[untyped-decorator]
    async def _edited(event: events.MessageEdited.Event) -> None:
        edit = await asyncio.to_thread(
            orchestrator.process_edit,
            source_id=chats[event.chat_id],
            message_id=event.message.id,
            text=event.raw_text or "",
            edited_at=event.message.edit_date or datetime.now(UTC),
        )
        if edit is not None and edit.executed:  # §25: alert only; never modify the position
            await on_edit(edit)

    return client
