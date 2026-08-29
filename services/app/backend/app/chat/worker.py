from __future__ import annotations

import asyncio
import logging

from ..config import get_settings
from ..database import chat_worker_engine, chat_worker_session_factory
from .execution import TurnExecutor
from .providers import FakeResponsesProvider, OpenAIResponsesProvider, ResponsesProvider

logger = logging.getLogger(__name__)


async def run() -> None:
    settings = get_settings()
    provider: ResponsesProvider
    if settings.chat_provider == "openai":
        provider = OpenAIResponsesProvider(settings)
    else:
        provider = FakeResponsesProvider(
            prefix=settings.chat_fake_response_prefix,
            delay_seconds=settings.chat_fake_stream_delay_seconds,
        )
    executor = TurnExecutor(
        session_factory=chat_worker_session_factory,
        provider=provider,
        settings=settings,
    )
    running: set[asyncio.Task[None]] = set()
    try:
        while True:
            while len(running) < settings.chat_worker_max_concurrency:
                turn_id = await executor.claim_next_turn()
                if turn_id is None:
                    break
                logger.info("executing turn %s", turn_id)
                running.add(asyncio.create_task(executor.execute(turn_id)))
            if not running:
                await asyncio.sleep(settings.chat_worker_poll_seconds)
                continue
            done, _ = await asyncio.wait(
                running,
                timeout=settings.chat_worker_poll_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            running.difference_update(done)
    finally:
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        await chat_worker_engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
