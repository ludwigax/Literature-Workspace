from __future__ import annotations

import json
import socket
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import Settings
from .context import ContextMaterializer
from .domain import TurnStatus, can_transition_turn
from .models import (
    ChatBranch,
    ChatSession,
    ConversationUnit,
    ModelOutputItem,
    ModelStep,
    ToolExecution,
    TurnRun,
    now_utc,
)
from .providers import ModelRequest, ResponsesProvider, UpstreamStreamError
from .service import ChatService
from .tool_config import get_literature_tool_config
from .tools import ToolContext, ToolRegistry, build_tool_registry


class TurnExecutor:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        provider: ResponsesProvider,
        settings: Settings,
        worker_id: str | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.provider = provider
        self.settings = settings
        self.worker_id = worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex[:12]}"
        self.context = ContextMaterializer()
        self.service = ChatService()
        self.tool_registry = tool_registry or build_tool_registry(settings)

    async def claim_next_turn(self) -> uuid.UUID | None:
        async with self.session_factory() as session, session.begin():
            candidates = (
                await session.scalars(
                select(TurnRun)
                .where(TurnRun.status == TurnStatus.WAITING.value)
                .order_by(TurnRun.created_at)
                .with_for_update(skip_locked=True)
                .limit(50)
                )
            ).all()
            turn: TurnRun | None = None
            for candidate in candidates:
                # Serialize capacity decisions for this principal across worker replicas.
                await session.execute(
                    text(
                        "SELECT pg_advisory_xact_lock("
                        "hashtextextended(CAST(:principal_id AS text), 0))"
                    ),
                    {"principal_id": str(candidate.owner_principal_id)},
                )
                running_count = int(
                    await session.scalar(
                        select(func.count(TurnRun.turn_id)).where(
                            TurnRun.owner_principal_id == candidate.owner_principal_id,
                            TurnRun.status.in_(
                                [
                                    TurnStatus.STARTING.value,
                                    TurnStatus.RUNNING_MODEL.value,
                                    TurnStatus.RUNNING_TOOLS.value,
                                    TurnStatus.INTERRUPT_REQUESTED.value,
                                ]
                            ),
                        )
                    )
                    or 0
                )
                if running_count < self.settings.principal_max_concurrency:
                    turn = candidate
                    break
            if turn is None:
                return None
            self._transition(turn, TurnStatus.STARTING)
            timestamp = now_utc()
            turn.lease_owner = self.worker_id
            turn.lease_expires_at = timestamp + timedelta(seconds=self.settings.turn_lease_seconds)
            turn.started_at = turn.started_at or timestamp
            await self.service.append_event(
                session,
                turn_id=turn.turn_id,
                event_type="turn.started",
                payload={"status": turn.status},
            )
            return turn.turn_id

    async def execute(self, turn_id: uuid.UUID) -> None:
        step_id: uuid.UUID | None = None
        try:
            async with self.session_factory() as session, session.begin():
                turn = await self._leased_turn(session, turn_id)
                working_input = await self.context.build(
                    session,
                    session_id=turn.session_id,
                    leaf_unit_id=turn.input_unit_id,
                )
            force_no_tools = False
            while True:
                step_id = await self._start_step(turn_id, working_input)
                response, partial, response_id, item_id, interrupted = await self._stream_step(
                    turn_id,
                    step_id,
                    working_input,
                    force_no_tools=force_no_tools,
                )
                if interrupted:
                    await self._interrupt_running(
                        turn_id,
                        step_id,
                        text=partial,
                        provider_response_id=response_id,
                        provider_item_id=item_id,
                    )
                    return
                output = response.get("output")
                if not isinstance(output, list):
                    raise RuntimeError("provider response output is not an array")
                function_calls = [
                    item
                    for item in output
                    if isinstance(item, dict) and item.get("type") == "function_call"
                ]
                if not function_calls:
                    await self._complete(turn_id, step_id, response)
                    return
                if force_no_tools:
                    raise RuntimeError("provider emitted tool calls while tools were disabled")
                remaining = await self._remaining_tool_calls(turn_id)
                source_items = await self._store_nonfinal_step(turn_id, step_id, response)
                if len(function_calls) > remaining:
                    force_no_tools = True
                    await self._tool_budget_exhausted(turn_id, len(function_calls), remaining)
                    continue
                outputs, tool_interrupted = await self._execute_tool_calls(
                    turn_id,
                    step_id,
                    function_calls,
                    source_items,
                )
                if tool_interrupted:
                    await self._interrupt_after_tools(turn_id)
                    return
                working_input = [*working_input, *output, *outputs]
        except Exception as error:
            await self._fail(turn_id, step_id, error)

    async def _start_step(
        self, turn_id: uuid.UUID, input_items: list[dict[str, Any]]
    ) -> uuid.UUID:
        async with self.session_factory() as session, session.begin():
            turn = await self._leased_turn(session, turn_id)
            next_ordinal = int(
                await session.scalar(
                    select(func.coalesce(func.max(ModelStep.ordinal), 0)).where(
                        ModelStep.turn_id == turn_id
                    )
                )
                or 0
            ) + 1
            step = ModelStep(
                turn_id=turn_id,
                ordinal=next_ordinal,
                status="RUNNING",
                provider=self.provider.name,
                model=self.settings.model,
                input_items_json=input_items,
                raw_response_json={},
                usage_json={},
            )
            session.add(step)
            await session.flush()
            if turn.status != TurnStatus.RUNNING_MODEL.value:
                self._transition(turn, TurnStatus.RUNNING_MODEL)
            await self.service.append_event(
                session,
                turn_id=turn_id,
                event_type="model.step.started",
                payload={"step_id": str(step.step_id), "ordinal": next_ordinal},
            )
            return step.step_id

    async def _stream_step(
        self,
        turn_id: uuid.UUID,
        step_id: uuid.UUID,
        input_items: list[dict[str, Any]],
        *,
        force_no_tools: bool,
    ) -> tuple[dict[str, Any], str, str, str, bool]:
        remaining = await self._remaining_tool_calls(turn_id)
        allow_tools = not force_no_tools and remaining > 0
        request = ModelRequest(
            model=self.settings.model,
            input_items=input_items,
            tools=self.tool_registry.schemas() if allow_tools else [],
            allow_tools=allow_tools,
            instructions=(
                "The tool-call budget is exhausted. Do not call tools. Return the best "
                "final answer using the information already available."
                if force_no_tools
                else None
            ),
        )
        completed_response: dict[str, Any] | None = None
        partial_chunks: list[str] = []
        response_id = ""
        item_id = ""
        interrupted = False
        async for event in self.provider.stream(request):
            response_object = event.payload.get("response")
            if isinstance(response_object, dict):
                response_id = str(response_object.get("id") or response_id)
            response_id = str(event.payload.get("response_id") or response_id)
            if event.event_type == "response.output_text.delta":
                partial_chunks.append(str(event.payload.get("delta") or ""))
                item_id = str(event.payload.get("item_id") or item_id)
            if event.event_type == "response.completed":
                candidate = event.payload.get("response")
                if not isinstance(candidate, dict):
                    raise RuntimeError("provider completed event has no response object")
                completed_response = candidate
            async with self.session_factory() as session, session.begin():
                streamed_turn = await self._leased_turn(session, turn_id)
                await self.service.append_event(
                    session,
                    turn_id=turn_id,
                    event_type=event.event_type,
                    payload={**event.payload, "step_id": str(step_id)},
                )
                interrupted = streamed_turn.status == TurnStatus.INTERRUPT_REQUESTED.value
            if interrupted:
                break
        if completed_response is None and not interrupted:
            raise RuntimeError("provider stream ended without response.completed")
        return (
            completed_response or {},
            "".join(partial_chunks),
            response_id,
            item_id,
            interrupted,
        )

    async def _remaining_tool_calls(self, turn_id: uuid.UUID) -> int:
        async with self.session_factory() as session:
            turn = await session.get(TurnRun, turn_id)
            if turn is None:
                raise RuntimeError("turn not found")
            return max(0, turn.max_tool_calls - turn.used_tool_calls)

    async def _store_nonfinal_step(
        self, turn_id: uuid.UUID, step_id: uuid.UUID, response: dict[str, Any]
    ) -> dict[str, uuid.UUID]:
        output = response.get("output")
        if not isinstance(output, list):
            raise RuntimeError("provider response output is not an array")
        source_items: dict[str, uuid.UUID] = {}
        async with self.session_factory() as session, session.begin():
            await self._leased_turn(session, turn_id)
            step = await session.get(ModelStep, step_id, with_for_update=True)
            if step is None or step.turn_id != turn_id:
                raise RuntimeError("model step disappeared during execution")
            for ordinal, payload in enumerate(output):
                if not isinstance(payload, dict):
                    raise RuntimeError("provider output item is not an object")
                item = ModelOutputItem(
                    step_id=step_id,
                    ordinal=ordinal,
                    provider_item_id=str(payload.get("id") or ""),
                    item_type=str(payload.get("type") or "unknown"),
                    payload_json=payload,
                )
                session.add(item)
                await session.flush()
                if payload.get("type") == "function_call":
                    source_items[str(payload.get("call_id") or "")] = item.item_id
            step.status = "COMPLETED"
            step.provider_response_id = str(response.get("id") or "")
            step.raw_response_json = response
            usage = response.get("usage")
            step.usage_json = usage if isinstance(usage, dict) else {}
            await self.service.append_event(
                session,
                turn_id=turn_id,
                event_type="model.step.completed",
                payload={
                    "step_id": str(step_id),
                    "provider_response_id": step.provider_response_id,
                    "has_tool_calls": True,
                },
            )
        return source_items

    async def _tool_budget_exhausted(
        self, turn_id: uuid.UUID, requested_calls: int, remaining: int
    ) -> None:
        async with self.session_factory() as session, session.begin():
            await self._leased_turn(session, turn_id)
            await self.service.append_event(
                session,
                turn_id=turn_id,
                event_type="turn.tool_budget_exhausted",
                payload={"requested_calls": requested_calls, "remaining": remaining},
            )

    async def _execute_tool_calls(
        self,
        turn_id: uuid.UUID,
        step_id: uuid.UUID,
        function_calls: list[dict[str, Any]],
        source_items: dict[str, uuid.UUID],
    ) -> tuple[list[dict[str, Any]], bool]:
        outputs: list[dict[str, Any]] = []
        interrupted = False
        for call in function_calls:
            call_id = str(call.get("call_id") or "")
            tool_name = str(call.get("name") or "")
            if not call_id or call_id not in source_items:
                raise RuntimeError("function call is missing a stable call_id")
            raw_arguments = call.get("arguments")
            try:
                arguments = (
                    json.loads(raw_arguments)
                    if isinstance(raw_arguments, str)
                    else raw_arguments
                )
                if not isinstance(arguments, dict):
                    raise ValueError("tool arguments must decode to an object")
            except Exception as error:
                arguments = {}
                argument_error: Exception | None = error
            else:
                argument_error = None
            try:
                tool = self.tool_registry.require(tool_name)
            except LookupError:
                tool = None

            async with self.session_factory() as session, session.begin():
                turn = await self._leased_turn(session, turn_id)
                if turn.status == TurnStatus.INTERRUPT_REQUESTED.value:
                    interrupted = True
                    break
                if turn.status == TurnStatus.RUNNING_MODEL.value:
                    self._transition(turn, TurnStatus.RUNNING_TOOLS)
                execution = ToolExecution(
                    turn_id=turn_id,
                    source_step_id=step_id,
                    source_item_id=source_items[call_id],
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments_json=arguments,
                    status="RUNNING",
                    result_json={},
                    error_json={},
                    attempt_count=1,
                    idempotency_key=f"{turn_id}:{call_id}",
                    started_at=now_utc(),
                )
                session.add(execution)
                turn.used_tool_calls += 1
                await session.flush()
                execution_id = execution.execution_id
                principal_id = turn.owner_principal_id
                await self.service.append_event(
                    session,
                    turn_id=turn_id,
                    event_type="tool.execution.started",
                    payload={
                        "execution_id": str(execution_id),
                        "call_id": call_id,
                        "tool_name": tool_name,
                    },
                )

            try:
                if tool is None:
                    raise LookupError(f"unknown tool: {tool_name}")
                if argument_error is not None:
                    raise ValueError(f"invalid tool arguments: {argument_error}")
                result = await tool.execute(
                    arguments,
                    ToolContext(
                        turn_id=turn_id,
                        principal_id=principal_id,
                        runtime_config=await self._runtime_tool_config(),
                    ),
                )
                model_result = {"ok": True, **result.data}
                status = "COMPLETED"
                error_payload: dict[str, Any] = {}
            except Exception as error:
                model_result = {
                    "ok": False,
                    "error": f"{type(error).__name__}: {error}",
                }
                status = "FAILED"
                error_payload = model_result

            async with self.session_factory() as session, session.begin():
                settled_turn = await session.get(TurnRun, turn_id, with_for_update=True)
                settled_execution = await session.get(
                    ToolExecution, execution_id, with_for_update=True
                )
                if settled_turn is None or settled_execution is None:
                    raise RuntimeError("tool execution disappeared")
                settled_execution.status = status
                settled_execution.result_json = (
                    {
                        "source_type": tool.source_type if tool is not None else "FUNCTION",
                        "output": model_result,
                    }
                    if status == "COMPLETED"
                    else {}
                )
                settled_execution.error_json = error_payload
                settled_execution.completed_at = now_utc()
                await self.service.append_event(
                    session,
                    turn_id=turn_id,
                    event_type=(
                        "tool.execution.completed"
                        if status == "COMPLETED"
                        else "tool.execution.failed"
                    ),
                    payload={
                        "execution_id": str(execution_id),
                        "call_id": call_id,
                        "tool_name": tool_name,
                        "ok": status == "COMPLETED",
                        "error": model_result.get("error"),
                    },
                )
                interrupted = (
                    settled_turn.status == TurnStatus.INTERRUPT_REQUESTED.value
                )
            outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(
                        model_result, ensure_ascii=False, separators=(",", ":")
                    ),
                }
            )
            if interrupted:
                break
        return outputs, interrupted

    async def _runtime_tool_config(self) -> dict[str, Any]:
        async with self.session_factory() as session:
            config, _ = await get_literature_tool_config(session, self.settings)
            return {
                "retrieval_mode": config.retrieval_mode,
                "retrieval_top_k": config.retrieval_top_k,
                "chunk_top_k_per_document": config.chunk_top_k_per_document,
                "doi_document_max_chars": config.doi_document_max_chars,
            }

    async def _interrupt_after_tools(self, turn_id: uuid.UUID) -> None:
        async with self.session_factory() as session, session.begin():
            turn = await session.get(TurnRun, turn_id, with_for_update=True)
            if turn is None or turn.status == TurnStatus.INTERRUPTED_PARTIAL.value:
                return
            if turn.status != TurnStatus.INTERRUPT_REQUESTED.value:
                raise RuntimeError("turn is not awaiting interruption")
            self._transition(turn, TurnStatus.INTERRUPTED_PARTIAL)
            turn.completion_reason = "user_interrupted_during_tools"
            turn.completed_at = now_utc()
            turn.lease_owner = None
            turn.lease_expires_at = None
            await self.service.append_event(
                session,
                turn_id=turn_id,
                event_type="turn.interrupted",
                payload={"status": turn.status, "final_unit_id": None},
            )

    async def _complete(
        self, turn_id: uuid.UUID, step_id: uuid.UUID, response: dict[str, Any]
    ) -> None:
        output = response.get("output")
        if not isinstance(output, list):
            raise RuntimeError("provider response output is not an array")
        function_calls = [
            item
            for item in output
            if isinstance(item, dict) and item.get("type") == "function_call"
        ]
        if function_calls:
            raise RuntimeError("provider emitted tool calls while tools were disabled")
        display_text = self._output_text(output)
        async with self.session_factory() as session, session.begin():
            turn = await self._leased_turn(session, turn_id)
            step = await session.get(ModelStep, step_id, with_for_update=True)
            if step is None or step.turn_id != turn_id:
                raise RuntimeError("model step disappeared during execution")
            if turn.status == TurnStatus.INTERRUPT_REQUESTED.value:
                await self._settle_interrupted(
                    session,
                    turn=turn,
                    step=step,
                    output=output,
                    display_text=display_text,
                    response=response,
                )
                return
            for ordinal, payload in enumerate(output):
                if not isinstance(payload, dict):
                    raise RuntimeError("provider output item is not an object")
                session.add(
                    ModelOutputItem(
                        step_id=step_id,
                        ordinal=ordinal,
                        provider_item_id=str(payload.get("id") or ""),
                        item_type=str(payload.get("type") or "unknown"),
                        payload_json=payload,
                    )
                )
            step.status = "COMPLETED"
            step.provider_response_id = str(response.get("id") or "")
            step.raw_response_json = response
            usage = response.get("usage")
            step.usage_json = usage if isinstance(usage, dict) else {}
            output_unit = ConversationUnit(
                session_id=turn.session_id,
                parent_unit_id=turn.input_unit_id,
                unit_type="MODEL_RESPONSE",
                status="SETTLED",
                turn_id=turn_id,
                model_step_id=step_id,
                display_text=display_text,
                content_json={"output_item_count": len(output)},
                interrupted=False,
            )
            session.add(output_unit)
            await session.flush()
            branch = await session.get(ChatBranch, turn.branch_id, with_for_update=True)
            if branch is None or branch.head_unit_id != turn.input_unit_id:
                raise RuntimeError("branch head changed while turn was running")
            branch.head_unit_id = output_unit.unit_id
            chat_session = await session.get(ChatSession, turn.session_id, with_for_update=True)
            if chat_session is None:
                raise RuntimeError("chat session disappeared during execution")
            chat_session.revision += 1
            chat_session.updated_at = now_utc()
            self._transition(turn, TurnStatus.COMPLETED)
            turn.final_unit_id = output_unit.unit_id
            turn.completion_reason = "model_completed"
            turn.completed_at = now_utc()
            turn.lease_owner = None
            turn.lease_expires_at = None
            await self.service.append_event(
                session,
                turn_id=turn_id,
                event_type="model.step.completed",
                payload={
                    "step_id": str(step_id),
                    "provider_response_id": step.provider_response_id,
                },
            )
            await self.service.append_event(
                session,
                turn_id=turn_id,
                event_type="turn.completed",
                payload={
                    "status": turn.status,
                    "final_unit_id": str(output_unit.unit_id),
                },
            )

    async def _interrupt_running(
        self,
        turn_id: uuid.UUID,
        step_id: uuid.UUID,
        *,
        text: str,
        provider_response_id: str,
        provider_item_id: str,
    ) -> None:
        item_id = provider_item_id or f"msg_interrupted_{uuid.uuid4().hex}"
        output: list[dict[str, Any]] = []
        if text:
            output.append(
                {
                    "id": item_id,
                    "type": "message",
                    "status": "incomplete",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": text, "annotations": []}
                    ],
                }
            )
        response = {
            "id": provider_response_id,
            "status": "incomplete",
            "output": output,
        }
        async with self.session_factory() as session, session.begin():
            turn = await session.get(TurnRun, turn_id, with_for_update=True)
            if turn is None or turn.status == TurnStatus.INTERRUPTED_PARTIAL.value:
                return
            if turn.status != TurnStatus.INTERRUPT_REQUESTED.value:
                raise RuntimeError("turn is not awaiting interruption")
            step = await session.get(ModelStep, step_id, with_for_update=True)
            if step is None:
                raise RuntimeError("model step disappeared during interruption")
            await self._settle_interrupted(
                session,
                turn=turn,
                step=step,
                output=output,
                display_text=text,
                response=response,
            )

    async def _settle_interrupted(
        self,
        session: AsyncSession,
        *,
        turn: TurnRun,
        step: ModelStep,
        output: list[Any],
        display_text: str,
        response: dict[str, Any],
    ) -> None:
        for ordinal, payload in enumerate(output):
            if not isinstance(payload, dict):
                continue
            session.add(
                ModelOutputItem(
                    step_id=step.step_id,
                    ordinal=ordinal,
                    provider_item_id=str(payload.get("id") or ""),
                    item_type=str(payload.get("type") or "unknown"),
                    payload_json=payload,
                )
            )
        step.status = "INTERRUPTED"
        step.provider_response_id = str(response.get("id") or "")
        step.raw_response_json = response
        usage = response.get("usage")
        step.usage_json = usage if isinstance(usage, dict) else {}
        output_unit: ConversationUnit | None = None
        if display_text:
            output_unit = ConversationUnit(
                session_id=turn.session_id,
                parent_unit_id=turn.input_unit_id,
                unit_type="MODEL_RESPONSE",
                status="SETTLED",
                turn_id=turn.turn_id,
                model_step_id=step.step_id,
                display_text=display_text,
                content_json={
                    "output_item_count": len(output),
                    "completion_reason": "user_interrupted",
                },
                interrupted=True,
            )
            session.add(output_unit)
            await session.flush()
            branch = await session.get(ChatBranch, turn.branch_id, with_for_update=True)
            if branch is None or branch.head_unit_id != turn.input_unit_id:
                raise RuntimeError("branch head changed while turn was interrupted")
            branch.head_unit_id = output_unit.unit_id
        chat_session = await session.get(ChatSession, turn.session_id, with_for_update=True)
        if chat_session is None:
            raise RuntimeError("chat session disappeared during interruption")
        chat_session.revision += 1
        chat_session.updated_at = now_utc()
        self._transition(turn, TurnStatus.INTERRUPTED_PARTIAL)
        turn.final_unit_id = output_unit.unit_id if output_unit else None
        turn.completion_reason = "user_interrupted"
        turn.completed_at = now_utc()
        turn.lease_owner = None
        turn.lease_expires_at = None
        await self.service.append_event(
            session,
            turn_id=turn.turn_id,
            event_type="model.step.interrupted",
            payload={"step_id": str(step.step_id), "partial": bool(display_text)},
        )
        await self.service.append_event(
            session,
            turn_id=turn.turn_id,
            event_type="turn.interrupted",
            payload={
                "status": turn.status,
                "final_unit_id": str(output_unit.unit_id) if output_unit else None,
            },
        )

    async def _fail(
        self, turn_id: uuid.UUID, step_id: uuid.UUID | None, error: Exception
    ) -> None:
        async with self.session_factory() as session, session.begin():
            turn = await session.get(TurnRun, turn_id, with_for_update=True)
            if turn is None or turn.status in {
                TurnStatus.COMPLETED.value,
                TurnStatus.INTERRUPTED_PARTIAL.value,
                TurnStatus.FAILED.value,
            }:
                return
            if step_id is not None:
                step = await session.get(ModelStep, step_id, with_for_update=True)
                if step is not None and step.status == "RUNNING":
                    step.status = "FAILED"
            turn.status = TurnStatus.FAILED.value
            turn.error = str(error)
            turn.completion_reason = (
                "upstream_stream_failed"
                if isinstance(error, UpstreamStreamError)
                else "execution_error"
            )
            turn.completed_at = now_utc()
            turn.lease_owner = None
            turn.lease_expires_at = None
            await self.service.append_event(
                session,
                turn_id=turn_id,
                event_type="turn.failed",
                payload={
                    "status": turn.status,
                    "reason": turn.completion_reason,
                    "error": str(error),
                },
            )

    async def _leased_turn(self, session: AsyncSession, turn_id: uuid.UUID) -> TurnRun:
        turn = await session.scalar(
            select(TurnRun).where(TurnRun.turn_id == turn_id).with_for_update()
        )
        if turn is None:
            raise RuntimeError("turn not found")
        if turn.lease_owner != self.worker_id:
            raise RuntimeError("turn lease is not owned by this worker")
        return turn

    @staticmethod
    def _transition(turn: TurnRun, target: TurnStatus) -> None:
        current = TurnStatus(turn.status)
        if not can_transition_turn(current, target):
            raise RuntimeError(f"invalid turn transition: {current.value} -> {target.value}")
        turn.status = target.value
        turn.updated_at = now_utc()

    @staticmethod
    def _output_text(output: list[Any]) -> str:
        texts: list[str] = []
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            texts.extend(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "output_text"
            )
        return "\n".join(text for text in texts if text).strip()
