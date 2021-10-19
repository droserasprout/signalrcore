import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple, Union

from aiosignalrcore.client_stream import ClientStream
from aiosignalrcore.exceptions import ServerError
from aiosignalrcore.handlers import InvocationHandler, StreamHandler
from aiosignalrcore.messages import (
    CancelInvocationMessage,
    CloseMessage,
    CompletionMessage,
    InvocationMessage,
    Message,
    MessageType,
    PingMessage,
    StreamInvocationMessage,
    StreamItemMessage,
)
from aiosignalrcore.protocol.abstract import Protocol
from aiosignalrcore.protocol.json import JsonProtocol
from aiosignalrcore.transport.websocket import WebsocketTransport

_logger = logging.getLogger(__name__)


class SignalRClient:
    def __init__(
        self,
        url: str,
        protocol: Optional[Protocol] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self._url = url
        self._protocol = protocol or JsonProtocol()
        self._headers = headers or {}

        self._handlers: List[Tuple[str, Callable]] = []
        self._stream_handlers: List[Union[StreamHandler, InvocationHandler]] = []

        self._transport = WebsocketTransport(
            url=self._url,
            protocol=self._protocol,
            callback=self._on_message,
            headers=self._headers,
        )
        self._error_callback: Optional[Callable[[CompletionMessage], Awaitable[None]]] = None

    async def run(self) -> None:

        # TODO: If auth...
        # _logger.debug("Starting connection ...")
        # self.token = self.auth_function()
        # _logger.debug("auth function result {0}".format(self.token))
        # self._headers["Authorization"] = "Bearer " + self.token

        _logger.debug("Connection started")
        return await self._transport.run()

    def on(self, event: str, callback: Callable[..., Awaitable[None]]) -> None:
        """Register a callback on the specified event
        Args:
            event (string):  Event name
            callback (Function): callback function,
                arguments will be binded
        """
        _logger.debug("Handler registered started {0}".format(event))
        self._handlers.append((event, callback))

    def on_open(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._transport.on_open(callback)

    def on_close(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._transport.on_close(callback)

    def on_error(self, callback: Callable[[CompletionMessage], Awaitable[None]]) -> None:
        self._error_callback = callback

    async def send(self, method: str, arguments: List[Dict[str, Any]], on_invocation=None) -> None:
        """Sends a message

        Args:
            method (string): Method name
            arguments (list|ClientStream): Method parameters
            on_invocation (function, optional): On invocation send callback
                will be raised on send server function ends. Defaults to None.

        Raises:
            ConnectionError: If hub is not ready to send
            TypeError: If arguments are invalid list or ClientStream
        """
        message = InvocationMessage(str(uuid.uuid4()), method, arguments, self._headers)

        if on_invocation:
            self._stream_handlers.append(InvocationHandler(message.invocation_id, on_invocation))

        await self._transport.send(message)

    async def _on_message(self, message: Message) -> None:
        # FIXME: When?
        if message.type == MessageType.invocation_binding_failure:  # type: ignore
            raise Exception
            # _logger.error(message)
            # self._on_error(message)

        elif isinstance(message, PingMessage):
            pass

        elif isinstance(message, InvocationMessage):
            await self._on_invocation_message(message)

        elif isinstance(message, CloseMessage):
            await self._on_close_message(message)

        elif isinstance(message, CompletionMessage):
            await self._on_completion_message(message)

        elif isinstance(message, StreamItemMessage):
            await self._on_stream_item_message(message)

        elif isinstance(message, StreamInvocationMessage):
            pass

        elif isinstance(message, CancelInvocationMessage):
            await self._on_cancel_invocation_message(message)

        else:
            raise NotImplementedError

    async def stream(self, event, event_params) -> StreamHandler:
        """Starts server streaming
            connection.stream(
            "Counter",
            [len(self.items), 500])\
            .subscribe({
                "next": self.on_next,
                "complete": self.on_complete,
                "error": self.on_error
            })
        Args:
            event (string): Method Name
            event_params (list): Method parameters

        Returns:
            [StreamHandler]: stream handler
        """
        invocation_id = str(uuid.uuid4())
        stream_obj = StreamHandler(event, invocation_id)
        self._stream_handlers.append(stream_obj)
        await self._transport.send(StreamInvocationMessage(invocation_id, event, event_params, headers=self._headers))
        return stream_obj

    @asynccontextmanager
    async def client_stream(self, target: str) -> AsyncIterator[ClientStream]:
        stream = ClientStream(self._transport, target)
        await stream.invoke()
        yield stream
        await stream.complete()

    async def _on_invocation_message(self, message: InvocationMessage) -> None:
        fired_handlers = list(filter(lambda h: h[0] == message.target, self._handlers))
        if len(fired_handlers) == 0:
            _logger.warning("event '{0}' hasn't fire any handler".format(message.target))
        for _, handler in fired_handlers:
            await handler(message.arguments)

    async def _on_completion_message(self, message: CompletionMessage) -> None:
        if message.error:
            if not self._error_callback:
                raise Exception
            await self._error_callback(message)

        # Send callbacks
        fired_stream_handlers = list(
            filter(
                lambda h: h.invocation_id == message.invocation_id,
                self._stream_handlers,
            )
        )

        # Stream callbacks
        for stream_handler in fired_stream_handlers:
            stream_handler.complete_callback(message)

        # unregister handler
        self._stream_handlers = list(
            filter(
                lambda h: h.invocation_id != message.invocation_id,
                self._stream_handlers,
            )
        )

    async def _on_stream_item_message(self, message: StreamItemMessage) -> None:
        fired_handlers = list(
            filter(
                lambda h: h.invocation_id == message.invocation_id,
                self._stream_handlers,
            )
        )
        if len(fired_handlers) == 0:
            _logger.warning("id '{0}' hasn't fire any stream handler".format(message.invocation_id))
        for handler in fired_handlers:
            assert isinstance(handler, StreamHandler)
            handler.next_callback(message.item)

    async def _on_cancel_invocation_message(self, message: CancelInvocationMessage) -> None:
        fired_handlers = list(
            filter(
                lambda h: h.invocation_id == message.invocation_id,
                self._stream_handlers,
            )
        )
        if len(fired_handlers) == 0:
            _logger.warning("id '{0}' hasn't fire any stream handler".format(message.invocation_id))

        for handler in fired_handlers:
            assert isinstance(handler, StreamHandler)
            handler.error_callback(message)

        # unregister handler
        self._stream_handlers = list(
            filter(
                lambda h: h.invocation_id != message.invocation_id,
                self._stream_handlers,
            )
        )

    async def _on_close_message(self, message: CloseMessage) -> None:
        if message.error:
            raise ServerError(message.error)
