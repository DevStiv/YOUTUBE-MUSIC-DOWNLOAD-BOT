import time
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import Message


class RateLimitMiddleware(BaseMiddleware):
    def __init__(self, limit_seconds: float = 2.5):
        self.limit_seconds = limit_seconds
        self.user_timestamps: Dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or not event.from_user:
            return await handler(event, data)

        # No limitar pagos ni comandos internos críticos
        if event.successful_payment:
            return await handler(event, data)

        user_id = event.from_user.id
        now = time.monotonic()
        last_time = self.user_timestamps.get(user_id, 0)

        if now - last_time < self.limit_seconds:
            await event.answer("⏳ <b>Procesando demasiado rápido.</b> Espera un par de segundos.")
            return

        self.user_timestamps[user_id] = now
        return await handler(event, data)
      
