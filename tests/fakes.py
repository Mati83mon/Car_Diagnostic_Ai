"""Test doubles: a scripted LLM, a fake serial port, and a fake J2534 library.

These exist so the whole stack -- graph, HITL gate, MCP tools, UDS session --
can be exercised deterministically with no API key, no network and no vehicle.
"""

from __future__ import annotations

import ctypes
from typing import Any, Iterable, Sequence

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class ScriptedChatModel(BaseChatModel):
    """A chat model that replays a fixed list of responses.

    Each entry is either a string (a plain answer) or a list of tool-call
    dicts. Lets a test drive the agent through an exact sequence of tool calls
    and assert on what the graph did with them.
    """

    responses: list[Any] = []
    calls: list[list[BaseMessage]] = []
    bound_tools: list[Any] = []

    def __init__(self, responses: Sequence[Any] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # Instance-level copies: class attributes would leak between tests.
        object.__setattr__(self, "responses", list(responses or []))
        object.__setattr__(self, "calls", [])
        object.__setattr__(self, "bound_tools", [])

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ScriptedChatModel:
        object.__setattr__(self, "bound_tools", list(tools))
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(list(messages))
        if not self.responses:
            message = AIMessage(content="(scripted model ran out of responses)")
        else:
            nxt = self.responses.pop(0)
            if isinstance(nxt, AIMessage):
                message = nxt
            elif isinstance(nxt, str):
                message = AIMessage(content=nxt)
            else:
                message = AIMessage(content="", tool_calls=list(nxt))
        return ChatResult(generations=[ChatGeneration(message=message)])

    @property
    def tool_names(self) -> list[str]:
        return [getattr(tool, "name", str(tool)) for tool in self.bound_tools]


def tool_call(name: str, args: dict[str, Any], call_id: str | None = None) -> dict[str, Any]:
    """Build a tool call for :class:`ScriptedChatModel`."""
    return {"name": name, "args": args, "id": call_id or f"call_{name}", "type": "tool_call"}


class FakeSerialPort:
    """A scripted ELM327 serial port.

    ``script`` maps a command (upper-cased, whitespace stripped) to the reply
    the adapter would send. Anything unmatched gets a bare prompt, which is how
    a real ELM327 answers a command it silently ignores.
    """

    def __init__(self, script: dict[str, str] | None = None, default: str = "") -> None:
        self.script = {k.upper(): v for k, v in (script or {}).items()}
        self.default = default
        self.written: list[str] = []
        self._buffer = bytearray()
        self.closed = False

    def write(self, data: bytes) -> int:
        command = data.decode("ascii", errors="replace").strip().upper()
        self.written.append(command)
        reply = self.script.get(command, self.default)
        self._buffer.extend(reply.encode("ascii"))
        self._buffer.extend(b">")
        return len(data)

    def read(self, size: int = 1) -> bytes:
        if not self._buffer:
            return b""
        chunk = bytes(self._buffer[:size])
        del self._buffer[:size]
        return chunk

    def close(self) -> None:
        self.closed = True

    @property
    def is_open(self) -> bool:
        return not self.closed


class FakePassThruLibrary:
    """A fake J2534 PassThru shared library.

    Records every call so a test can assert the flow-control filter really was
    installed -- the omission that silently breaks multi-frame reads.
    """

    def __init__(self, *, responses: Iterable[bytes] = (), open_error: int = 0) -> None:
        self.calls: list[str] = []
        self.queued: list[bytes] = list(responses)
        self.open_error = open_error
        self.written: list[bytes] = []
        self.filters_installed = 0
        self.closed = False

    def PassThruOpen(self, name: Any, device_id: Any) -> int:  # noqa: N802
        self.calls.append("open")
        if self.open_error:
            return self.open_error
        device_id._obj.value = 1
        return 0

    def PassThruConnect(
        self, device: Any, protocol: int, flags: int, baud: int, channel: Any  # noqa: N802
    ) -> int:
        self.calls.append(f"connect:{protocol}:{baud}")
        channel._obj.value = 2
        return 0

    def PassThruStartMsgFilter(
        self,
        channel: Any,
        kind: int,
        mask: Any,  # noqa: N802
        pattern: Any,
        flow: Any,
        filter_id: Any,
    ) -> int:
        self.calls.append(f"filter:{kind}")
        self.filters_installed += 1
        filter_id._obj.value = 3
        return 0

    def PassThruWriteMsgs(
        self, channel: Any, message: Any, count: Any, timeout: int  # noqa: N802
    ) -> int:
        self.calls.append("write")
        self.written.append(message._obj.get_data())
        return 0

    def PassThruReadMsgs(
        self, channel: Any, message: Any, count: Any, timeout: int  # noqa: N802
    ) -> int:
        self.calls.append("read")
        if not self.queued:
            count._obj.value = 0
            return 0x10  # ERR_BUFFER_EMPTY
        payload = self.queued.pop(0)
        message._obj.ProtocolID = 6
        message._obj.RxStatus = 0
        message._obj.set_data(payload)
        count._obj.value = 1
        return 0

    def PassThruIoctl(self, channel: Any, ioctl_id: int, inp: Any, out: Any) -> int:  # noqa: N802
        self.calls.append(f"ioctl:{ioctl_id}")
        return 0

    def PassThruStopMsgFilter(self, channel: Any, filter_id: Any) -> int:  # noqa: N802
        self.calls.append("stop_filter")
        return 0

    def PassThruDisconnect(self, channel: Any) -> int:  # noqa: N802
        self.calls.append("disconnect")
        return 0

    def PassThruClose(self, device: Any) -> int:  # noqa: N802
        self.calls.append("close")
        self.closed = True
        return 0

    def PassThruGetLastError(self, buffer: Any) -> int:  # noqa: N802
        buffer.value = b"fake library error"
        return 0


__all__ = [
    "ScriptedChatModel",
    "tool_call",
    "FakeSerialPort",
    "FakePassThruLibrary",
]
