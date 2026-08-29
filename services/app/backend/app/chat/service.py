from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from .models import (
    ChatBranch,
    ChatSession,
    ConversationUnit,
    ToolExecution,
    TurnEvent,
    TurnRun,
)


class NotFoundError(RuntimeError):
    pass


class ConflictError(RuntimeError):
    pass


def now_utc() -> datetime:
    return datetime.now(UTC)


class ChatService:
    async def create_session(
        self, session: AsyncSession, *, owner_principal_id: uuid.UUID, title: str
    ) -> dict[str, Any]:
        chat_session = ChatSession(
            owner_principal_id=owner_principal_id,
            title=self._required_text(title, "title", max_length=300),
            status="ACTIVE",
            revision=0,
        )
        session.add(chat_session)
        await session.flush()
        branch = ChatBranch(session_id=chat_session.session_id, name="main")
        session.add(branch)
        await session.commit()
        return self.session_dict(chat_session, default_branch_id=branch.branch_id)

    async def list_sessions(
        self, session: AsyncSession, *, owner_principal_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        rows = (
            await session.scalars(
                select(ChatSession)
                .where(
                    ChatSession.owner_principal_id == owner_principal_id,
                    ChatSession.status != "DELETED",
                )
                .order_by(ChatSession.updated_at.desc())
            )
        ).all()
        result: list[dict[str, Any]] = []
        for row in rows:
            branch_id = await session.scalar(
                select(ChatBranch.branch_id)
                .where(ChatBranch.session_id == row.session_id)
                .order_by(ChatBranch.created_at)
                .limit(1)
            )
            result.append(self.session_dict(row, default_branch_id=branch_id))
        return result

    async def get_session(
        self,
        session: AsyncSession,
        *,
        owner_principal_id: uuid.UUID,
        session_id: uuid.UUID,
    ) -> dict[str, Any]:
        chat_session = await self._owned_session(
            session, owner_principal_id=owner_principal_id, session_id=session_id
        )
        branch_id = await session.scalar(
            select(ChatBranch.branch_id)
            .where(ChatBranch.session_id == session_id)
            .order_by(ChatBranch.created_at)
            .limit(1)
        )
        return self.session_dict(chat_session, default_branch_id=branch_id)

    async def create_turn(
        self,
        session: AsyncSession,
        *,
        owner_principal_id: uuid.UUID,
        session_id: uuid.UUID,
        content: str,
        branch_id: uuid.UUID | None,
        parent_unit_id: uuid.UUID | None,
        base_revision: int | None,
        max_tool_calls: int,
    ) -> dict[str, Any]:
        if max_tool_calls < 0 or max_tool_calls > get_settings().chat_max_tool_calls_ceiling:
            raise ValueError("max_tool_calls is outside the configured range")
        text = self._required_text(content, "content")
        chat_session = await session.scalar(
            select(ChatSession)
            .where(
                ChatSession.session_id == session_id,
                ChatSession.owner_principal_id == owner_principal_id,
                ChatSession.status == "ACTIVE",
            )
            .with_for_update()
        )
        if chat_session is None:
            raise NotFoundError("chat session not found")
        if base_revision is not None and chat_session.revision != base_revision:
            raise ConflictError("chat session revision conflict")
        await self._ensure_no_active_turn(session, session_id=session_id)
        branch = await self._branch_for_update(session, session_id=session_id, branch_id=branch_id)
        selected_parent = parent_unit_id if parent_unit_id is not None else branch.head_unit_id
        if selected_parent is not None:
            parent = await session.get(ConversationUnit, selected_parent)
            if parent is None or parent.session_id != session_id or parent.status != "SETTLED":
                raise ConflictError("parent conversation unit is not a settled session unit")
        if selected_parent != branch.head_unit_id:
            branch = await self._new_branch(
                session,
                session_id=session_id,
                created_from_unit_id=selected_parent,
            )

        input_unit = ConversationUnit(
            session_id=session_id,
            parent_unit_id=selected_parent,
            unit_type="USER_INPUT",
            status="SETTLED",
            display_text=text,
            content_json={"text": text, "resource_refs": []},
        )
        session.add(input_unit)
        await session.flush()
        turn = TurnRun(
            session_id=session_id,
            branch_id=branch.branch_id,
            owner_principal_id=owner_principal_id,
            input_unit_id=input_unit.unit_id,
            status="WAITING",
            max_tool_calls=max_tool_calls,
            used_tool_calls=0,
            completion_reason="",
            error="",
        )
        session.add(turn)
        await session.flush()
        input_unit.turn_id = turn.turn_id
        if branch.root_unit_id is None:
            branch.root_unit_id = input_unit.unit_id
        branch.head_unit_id = input_unit.unit_id
        chat_session.revision += 1
        chat_session.updated_at = now_utc()
        await self.append_event(
            session,
            turn_id=turn.turn_id,
            event_type="turn.queued",
            payload={"status": "WAITING"},
        )
        try:
            await session.commit()
        except IntegrityError as error:
            await session.rollback()
            raise ConflictError("chat session already has a nonterminal turn") from error
        return self.turn_dict(turn)

    async def edit_and_regenerate(
        self,
        session: AsyncSession,
        *,
        owner_principal_id: uuid.UUID,
        unit_id: uuid.UUID,
        content: str,
        base_revision: int | None,
        max_tool_calls: int,
    ) -> dict[str, Any]:
        self._validate_tool_budget(max_tool_calls)
        text = self._required_text(content, "content")
        source = await session.scalar(
            select(ConversationUnit)
            .join(ChatSession, ChatSession.session_id == ConversationUnit.session_id)
            .where(
                ConversationUnit.unit_id == unit_id,
                ConversationUnit.unit_type == "USER_INPUT",
                ConversationUnit.status == "SETTLED",
                ChatSession.owner_principal_id == owner_principal_id,
                ChatSession.status == "ACTIVE",
            )
        )
        if source is None:
            raise NotFoundError("editable user unit not found")
        chat_session = await self._lock_active_session(
            session,
            owner_principal_id=owner_principal_id,
            session_id=source.session_id,
            base_revision=base_revision,
        )
        await self._ensure_no_active_turn(session, session_id=source.session_id)
        branch = await self._new_branch(
            session,
            session_id=source.session_id,
            created_from_unit_id=source.unit_id,
        )
        replacement = ConversationUnit(
            session_id=source.session_id,
            parent_unit_id=source.parent_unit_id,
            unit_type="USER_INPUT",
            status="SETTLED",
            display_text=text,
            content_json={
                "text": text,
                "resource_refs": source.content_json.get("resource_refs", []),
                "edited_from_unit_id": str(source.unit_id),
            },
        )
        session.add(replacement)
        await session.flush()
        branch.root_unit_id = (
            replacement.unit_id
            if replacement.parent_unit_id is None
            else await self._root_unit_id(session, replacement.parent_unit_id)
        )
        branch.head_unit_id = replacement.unit_id
        turn = await self._add_turn(
            session,
            chat_session=chat_session,
            branch=branch,
            owner_principal_id=owner_principal_id,
            input_unit=replacement,
            max_tool_calls=max_tool_calls,
        )
        await self._commit_turn(session)
        return self.turn_dict(turn)

    async def regenerate(
        self,
        session: AsyncSession,
        *,
        owner_principal_id: uuid.UUID,
        unit_id: uuid.UUID,
        base_revision: int | None,
        max_tool_calls: int,
    ) -> dict[str, Any]:
        self._validate_tool_budget(max_tool_calls)
        selected = await session.scalar(
            select(ConversationUnit)
            .join(ChatSession, ChatSession.session_id == ConversationUnit.session_id)
            .where(
                ConversationUnit.unit_id == unit_id,
                ConversationUnit.status == "SETTLED",
                ChatSession.owner_principal_id == owner_principal_id,
                ChatSession.status == "ACTIVE",
            )
        )
        if selected is None:
            raise NotFoundError("conversation unit not found")
        source = selected
        if selected.unit_type == "MODEL_RESPONSE":
            if selected.parent_unit_id is None:
                raise ConflictError("model response has no input unit")
            parent = await session.get(ConversationUnit, selected.parent_unit_id)
            if parent is None:
                raise ConflictError("model response input unit is unavailable")
            source = parent
        if source.unit_type != "USER_INPUT":
            raise ConflictError("regeneration requires a user input or its model response")
        chat_session = await self._lock_active_session(
            session,
            owner_principal_id=owner_principal_id,
            session_id=source.session_id,
            base_revision=base_revision,
        )
        await self._ensure_no_active_turn(session, session_id=source.session_id)
        branch = await self._new_branch(
            session,
            session_id=source.session_id,
            created_from_unit_id=selected.unit_id,
        )
        branch.root_unit_id = await self._root_unit_id(session, source.unit_id)
        branch.head_unit_id = source.unit_id
        turn = await self._add_turn(
            session,
            chat_session=chat_session,
            branch=branch,
            owner_principal_id=owner_principal_id,
            input_unit=source,
            max_tool_calls=max_tool_calls,
        )
        await self._commit_turn(session)
        return self.turn_dict(turn)

    async def get_turn(
        self,
        session: AsyncSession,
        *,
        owner_principal_id: uuid.UUID,
        turn_id: uuid.UUID,
    ) -> dict[str, Any]:
        turn = await session.scalar(
            select(TurnRun).where(
                TurnRun.turn_id == turn_id,
                TurnRun.owner_principal_id == owner_principal_id,
            )
        )
        if turn is None:
            raise NotFoundError("turn not found")
        return self.turn_dict(turn)

    async def request_interrupt(
        self,
        session: AsyncSession,
        *,
        owner_principal_id: uuid.UUID,
        turn_id: uuid.UUID,
    ) -> dict[str, Any]:
        turn = await session.scalar(
            select(TurnRun)
            .where(
                TurnRun.turn_id == turn_id,
                TurnRun.owner_principal_id == owner_principal_id,
            )
            .with_for_update()
        )
        if turn is None:
            raise NotFoundError("turn not found")
        if turn.status in {"COMPLETED", "INTERRUPTED_PARTIAL", "FAILED"}:
            return self.turn_dict(turn)
        if turn.status == "INTERRUPT_REQUESTED":
            return self.turn_dict(turn)
        previous_status = turn.status
        await self.append_event(
            session,
            turn_id=turn_id,
            event_type="turn.interrupt_requested",
            payload={"previous_status": previous_status},
        )
        if previous_status in {"WAITING", "STARTING"}:
            turn.status = "INTERRUPTED_PARTIAL"
            turn.completion_reason = "user_interrupted_before_output"
            turn.completed_at = now_utc()
            turn.lease_owner = None
            turn.lease_expires_at = None
            await self.append_event(
                session,
                turn_id=turn_id,
                event_type="turn.interrupted",
                payload={"status": turn.status, "final_unit_id": None},
            )
        else:
            turn.status = "INTERRUPT_REQUESTED"
        await session.commit()
        return self.turn_dict(turn)

    async def get_events(
        self,
        session: AsyncSession,
        *,
        owner_principal_id: uuid.UUID,
        turn_id: uuid.UUID,
        after: int,
    ) -> list[dict[str, Any]]:
        await self.get_turn(
            session, owner_principal_id=owner_principal_id, turn_id=turn_id
        )
        events = (
            await session.scalars(
                select(TurnEvent)
                .where(TurnEvent.turn_id == turn_id, TurnEvent.sequence_no > after)
                .order_by(TurnEvent.sequence_no)
            )
        ).all()
        return [self.event_dict(event) for event in events]

    async def get_tool_executions(
        self,
        session: AsyncSession,
        *,
        owner_principal_id: uuid.UUID,
        turn_id: uuid.UUID,
    ) -> list[dict[str, Any]]:
        await self.get_turn(
            session, owner_principal_id=owner_principal_id, turn_id=turn_id
        )
        values = (
            await session.scalars(
                select(ToolExecution)
                .where(ToolExecution.turn_id == turn_id)
                .order_by(ToolExecution.created_at, ToolExecution.execution_id)
            )
        ).all()
        return [self.tool_execution_dict(value) for value in values]

    async def graph(
        self,
        session: AsyncSession,
        *,
        owner_principal_id: uuid.UUID,
        session_id: uuid.UUID,
    ) -> dict[str, Any]:
        chat_session = await self._owned_session(
            session, owner_principal_id=owner_principal_id, session_id=session_id
        )
        branches = (
            await session.scalars(
                select(ChatBranch)
                .where(ChatBranch.session_id == session_id)
                .order_by(ChatBranch.created_at)
            )
        ).all()
        units = (
            await session.scalars(
                select(ConversationUnit)
                .where(ConversationUnit.session_id == session_id)
                .order_by(ConversationUnit.created_at)
            )
        ).all()
        turns = (
            await session.scalars(
                select(TurnRun)
                .where(TurnRun.session_id == session_id)
                .order_by(TurnRun.created_at)
            )
        ).all()
        return {
            "session": self.session_dict(chat_session),
            "branches": [self.branch_dict(branch) for branch in branches],
            "units": [self.unit_dict(unit) for unit in units],
            "turns": [self.turn_dict(turn) for turn in turns],
        }

    @staticmethod
    async def append_event(
        session: AsyncSession,
        *,
        turn_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> TurnEvent:
        sequence = int(
            await session.scalar(
                select(func.coalesce(func.max(TurnEvent.sequence_no), 0)).where(
                    TurnEvent.turn_id == turn_id
                )
            )
            or 0
        ) + 1
        event = TurnEvent(
            turn_id=turn_id,
            sequence_no=sequence,
            event_type=event_type,
            payload_json=payload,
        )
        session.add(event)
        await session.flush()
        return event

    @staticmethod
    def session_dict(
        value: ChatSession, *, default_branch_id: uuid.UUID | None = None
    ) -> dict[str, Any]:
        return {
            "session_id": str(value.session_id),
            "owner_principal_id": str(value.owner_principal_id),
            "title": value.title,
            "status": value.status,
            "revision": value.revision,
            "default_branch_id": str(default_branch_id) if default_branch_id else None,
            "created_at": value.created_at.isoformat(),
            "updated_at": value.updated_at.isoformat(),
        }

    @staticmethod
    def branch_dict(value: ChatBranch) -> dict[str, Any]:
        return {
            "branch_id": str(value.branch_id),
            "session_id": str(value.session_id),
            "name": value.name,
            "root_unit_id": str(value.root_unit_id) if value.root_unit_id else None,
            "head_unit_id": str(value.head_unit_id) if value.head_unit_id else None,
            "created_from_unit_id": (
                str(value.created_from_unit_id) if value.created_from_unit_id else None
            ),
        }

    @staticmethod
    def unit_dict(value: ConversationUnit) -> dict[str, Any]:
        return {
            "unit_id": str(value.unit_id),
            "session_id": str(value.session_id),
            "parent_unit_id": str(value.parent_unit_id) if value.parent_unit_id else None,
            "unit_type": value.unit_type,
            "status": value.status,
            "turn_id": str(value.turn_id) if value.turn_id else None,
            "model_step_id": str(value.model_step_id) if value.model_step_id else None,
            "display_text": value.display_text,
            "content": value.content_json,
            "interrupted": value.interrupted,
            "created_at": value.created_at.isoformat(),
        }

    @staticmethod
    def turn_dict(value: TurnRun) -> dict[str, Any]:
        return {
            "turn_id": str(value.turn_id),
            "session_id": str(value.session_id),
            "branch_id": str(value.branch_id),
            "input_unit_id": str(value.input_unit_id),
            "final_unit_id": str(value.final_unit_id) if value.final_unit_id else None,
            "status": value.status,
            "max_tool_calls": value.max_tool_calls,
            "used_tool_calls": value.used_tool_calls,
            "completion_reason": value.completion_reason,
            "error": value.error,
            "created_at": value.created_at.isoformat(),
            "started_at": value.started_at.isoformat() if value.started_at else None,
            "completed_at": value.completed_at.isoformat() if value.completed_at else None,
        }

    @staticmethod
    def event_dict(value: TurnEvent) -> dict[str, Any]:
        return {
            "event_id": value.event_id,
            "turn_id": str(value.turn_id),
            "sequence": value.sequence_no,
            "type": value.event_type,
            "payload": value.payload_json,
            "created_at": value.created_at.isoformat(),
        }

    @staticmethod
    def tool_execution_dict(value: ToolExecution) -> dict[str, Any]:
        return {
            "execution_id": str(value.execution_id),
            "turn_id": str(value.turn_id),
            "source_step_id": str(value.source_step_id),
            "source_item_id": str(value.source_item_id),
            "call_id": value.call_id,
            "tool_name": value.tool_name,
            "arguments": value.arguments_json,
            "status": value.status,
            "result": value.result_json,
            "error": value.error_json,
            "attempt_count": value.attempt_count,
            "created_at": value.created_at.isoformat(),
            "started_at": value.started_at.isoformat() if value.started_at else None,
            "completed_at": value.completed_at.isoformat() if value.completed_at else None,
        }

    @staticmethod
    async def _owned_session(
        session: AsyncSession,
        *,
        owner_principal_id: uuid.UUID,
        session_id: uuid.UUID,
    ) -> ChatSession:
        value = await session.scalar(
            select(ChatSession).where(
                ChatSession.session_id == session_id,
                ChatSession.owner_principal_id == owner_principal_id,
                ChatSession.status != "DELETED",
            )
        )
        if value is None:
            raise NotFoundError("chat session not found")
        return value

    @staticmethod
    async def _branch_for_update(
        session: AsyncSession,
        *,
        session_id: uuid.UUID,
        branch_id: uuid.UUID | None,
    ) -> ChatBranch:
        query = select(ChatBranch).where(ChatBranch.session_id == session_id)
        if branch_id is not None:
            query = query.where(ChatBranch.branch_id == branch_id)
        else:
            query = query.order_by(ChatBranch.created_at).limit(1)
        value = await session.scalar(query.with_for_update())
        if value is None:
            raise NotFoundError("chat branch not found")
        return value

    async def _lock_active_session(
        self,
        session: AsyncSession,
        *,
        owner_principal_id: uuid.UUID,
        session_id: uuid.UUID,
        base_revision: int | None,
    ) -> ChatSession:
        chat_session = await session.scalar(
            select(ChatSession)
            .where(
                ChatSession.session_id == session_id,
                ChatSession.owner_principal_id == owner_principal_id,
                ChatSession.status == "ACTIVE",
            )
            .with_for_update()
        )
        if chat_session is None:
            raise NotFoundError("chat session not found")
        if base_revision is not None and chat_session.revision != base_revision:
            raise ConflictError("chat session revision conflict")
        return chat_session

    @staticmethod
    async def _ensure_no_active_turn(
        session: AsyncSession, *, session_id: uuid.UUID
    ) -> None:
        active_turn_id = await session.scalar(
            select(TurnRun.turn_id).where(
                TurnRun.session_id == session_id,
                TurnRun.status.in_(
                    [
                        "WAITING",
                        "STARTING",
                        "RUNNING_MODEL",
                        "RUNNING_TOOLS",
                        "INTERRUPT_REQUESTED",
                    ]
                ),
            )
        )
        if active_turn_id is not None:
            raise ConflictError(f"chat session already has active turn {active_turn_id}")

    async def _new_branch(
        self,
        session: AsyncSession,
        *,
        session_id: uuid.UUID,
        created_from_unit_id: uuid.UUID | None,
    ) -> ChatBranch:
        branch = ChatBranch(
            session_id=session_id,
            name=f"branch-{uuid.uuid4().hex[:12]}",
            created_from_unit_id=created_from_unit_id,
            head_unit_id=created_from_unit_id,
            root_unit_id=(
                await self._root_unit_id(session, created_from_unit_id)
                if created_from_unit_id is not None
                else None
            ),
        )
        session.add(branch)
        await session.flush()
        return branch

    @staticmethod
    async def _root_unit_id(session: AsyncSession, unit_id: uuid.UUID) -> uuid.UUID:
        current_id = unit_id
        seen: set[uuid.UUID] = set()
        while True:
            if current_id in seen:
                raise RuntimeError("conversation unit cycle detected")
            seen.add(current_id)
            unit = await session.get(ConversationUnit, current_id)
            if unit is None:
                raise RuntimeError("conversation root is unavailable")
            if unit.parent_unit_id is None:
                return unit.unit_id
            current_id = unit.parent_unit_id

    async def _add_turn(
        self,
        session: AsyncSession,
        *,
        chat_session: ChatSession,
        branch: ChatBranch,
        owner_principal_id: uuid.UUID,
        input_unit: ConversationUnit,
        max_tool_calls: int,
    ) -> TurnRun:
        turn = TurnRun(
            session_id=chat_session.session_id,
            branch_id=branch.branch_id,
            owner_principal_id=owner_principal_id,
            input_unit_id=input_unit.unit_id,
            status="WAITING",
            max_tool_calls=max_tool_calls,
            used_tool_calls=0,
            completion_reason="",
            error="",
        )
        session.add(turn)
        await session.flush()
        if input_unit.turn_id is None:
            input_unit.turn_id = turn.turn_id
        chat_session.revision += 1
        chat_session.updated_at = now_utc()
        await self.append_event(
            session,
            turn_id=turn.turn_id,
            event_type="turn.queued",
            payload={"status": "WAITING"},
        )
        return turn

    @staticmethod
    async def _commit_turn(session: AsyncSession) -> None:
        try:
            await session.commit()
        except IntegrityError as error:
            await session.rollback()
            raise ConflictError("chat session already has a nonterminal turn") from error

    @staticmethod
    def _validate_tool_budget(max_tool_calls: int) -> None:
        if max_tool_calls < 0 or max_tool_calls > get_settings().chat_max_tool_calls_ceiling:
            raise ValueError("max_tool_calls is outside the configured range")

    @staticmethod
    def _required_text(value: str, field: str, *, max_length: int = 100_000) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{field} is required")
        if len(normalized) > max_length:
            raise ValueError(f"{field} is too long")
        return normalized


chat_service = ChatService()
