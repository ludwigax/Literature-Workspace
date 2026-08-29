from backend.app.domain import TurnStatus, can_transition_turn


def test_turn_state_machine_rejects_terminal_reentry() -> None:
    assert can_transition_turn(TurnStatus.WAITING, TurnStatus.STARTING)
    assert can_transition_turn(TurnStatus.RUNNING_MODEL, TurnStatus.COMPLETED)
    assert not can_transition_turn(TurnStatus.COMPLETED, TurnStatus.RUNNING_MODEL)
