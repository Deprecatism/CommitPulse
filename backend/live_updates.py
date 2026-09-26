import asyncio
import logging

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect


class LiveUpdateHub:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._event_loop: asyncio.AbstractEventLoop | None = None

    def bind_event_loop(self, event_loop: asyncio.AbstractEventLoop) -> None:
        self._event_loop = event_loop

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def broadcast(self, event: dict[str, object]) -> None:
        disconnected = []
        for websocket in tuple(self._connections):
            try:
                await websocket.send_json(event)
            except (RuntimeError, WebSocketDisconnect):
                disconnected.append(websocket)
        for websocket in disconnected:
            self.disconnect(websocket)

    def broadcast_from_thread(self, event: dict[str, object]) -> None:
        event_loop = self._event_loop
        if event_loop is None or event_loop.is_closed():
            return

        future = asyncio.run_coroutine_threadsafe(self.broadcast(event), event_loop)
        future.add_done_callback(self._log_broadcast_error)

    @staticmethod
    def _log_broadcast_error(future: object) -> None:
        if hasattr(future, "exception"):
            error = future.exception()
            if error is not None:
                logging.getLogger(__name__).warning(
                    "Could not broadcast live metrics update: %s", error
                )
