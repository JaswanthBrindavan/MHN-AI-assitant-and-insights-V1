"""Provider-neutral tool-calling vocabulary — pure, stdlib only.

Anthropic and OpenAI-compatible endpoints express tool use with incompatible
wire formats. This module is the single internal shape both adapters translate
to and from, so nothing above the adapter layer knows which provider is live.

Keep this module free of I/O, httpx, and vendor SDK imports.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolSpec:
    """A tool offered to the model. ``input_schema`` is JSON Schema."""

    name: str
    description: str
    input_schema: dict


@dataclass(frozen=True)
class ToolCall:
    """The model asking for a tool to run. ``id`` correlates the result."""

    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ToolResult:
    """The outcome of one tool call. ``content`` is a JSON string."""

    call_id: str
    content: str
    is_error: bool = False
    # Whether the values in this result may be treated as SOURCES by the
    # numeric-fidelity guard. True for a database read: the value came from
    # the reader's record, so quoting it is faithful. FALSE for anything a
    # model produced — a vision transcription of a lab report is a guess, and
    # letting it into `sources` would have the fidelity guard authorise the
    # very misreads it exists to catch (INR 1.0 read as 10.0, potassium 5.9
    # read as 59).
    trusted_values: bool = True


@dataclass(frozen=True)
class UserMessage:
    content: str
    # Provider-neutral attachments (currently images). Each is a dict in the
    # shape app/vision/service.py builds; the adapters translate it to their
    # own wire format. Kept OUT of `content` so nothing upstream of the
    # adapters has to know an image is involved.
    attachments: tuple[dict, ...] = ()


@dataclass(frozen=True)
class AssistantMessage:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class ToolResultMessage:
    """ALL results from one assistant turn, together.

    Splitting parallel results across several messages silently trains the
    model to stop making parallel calls — holding them in one message makes
    that mistake unrepresentable.
    """

    results: tuple[ToolResult, ...]


Message = UserMessage | AssistantMessage | ToolResultMessage


@dataclass(frozen=True)
class LLMTurn:
    """One model response.

    stop_reason: "end_turn" | "tool_use" | "max_tokens" | "refusal"
    """

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: str = "end_turn"
    usage: dict | None = None

    @property
    def wants_tools(self) -> bool:
        return self.stop_reason == "tool_use" and bool(self.tool_calls)
