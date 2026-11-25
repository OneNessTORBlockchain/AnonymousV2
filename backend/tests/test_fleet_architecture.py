"""The squad contract: one squad = five specialists, at most five squads.

These tests pin the single source of truth (``FLEET_GROUP_ROLES``) and make sure
no code path, schema or prompt can reintroduce a sixth specialist or a 26th
agent.  They also assert that the Learning Board API is actually mounted.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import FLEET_GROUP_ROLES, FLEET_GROUP_SIZE, FLEET_MAX_GROUPS
from app.core.agents import AGENT_META
from app.core.swarm import (
    DEFAULT_LANES,
    FLEET_ROLE_ORDER,
    MAX_LANES,
    SwarmManager,
    swarm_prompt_block,
)
from app.core.tools import AGENT_TOOLS, DELEGATABLE
from app.schemas import ChatRequest, SwarmStartIn


def _manager(**overrides: object) -> SimpleNamespace:
    settings = SimpleNamespace(
        kali_workdir="/work",
        fleet_models=["model-a", "model-b"],
        planner_model="model-a",
        fleet_groups=1,
        fleet_agents_per_model=2,
        fleet_rounds=2,
        fleet_max_rounds=50,
        fleet_unlimited_rounds=10_000,
        memory_digest_limit=12,
        swarm_knowledge_report=True,
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    return SimpleNamespace(settings=settings)


def test_one_squad_is_exactly_five_roles() -> None:
    assert FLEET_GROUP_ROLES == ("recon", "web", "exploit", "coder", "reporter")
    assert FLEET_GROUP_SIZE == 5
    assert FLEET_ROLE_ORDER == FLEET_GROUP_ROLES
    assert DEFAULT_LANES == FLEET_GROUP_SIZE
    assert FLEET_MAX_GROUPS == 5
    assert MAX_LANES == FLEET_MAX_GROUPS * FLEET_GROUP_SIZE == 25


@pytest.mark.parametrize("groups", [1, 2, 3, 4, 5])
def test_squads_produce_five_agents_each(groups: int) -> None:
    lanes = SwarmManager._fleet_roster(_manager(), "run", None, groups)
    assert len(lanes) == groups * FLEET_GROUP_SIZE
    assert [lane.category for lane in lanes] == list(FLEET_GROUP_ROLES) * groups
    assert {lane.group for lane in lanes} == set(range(1, groups + 1))
    assert len({lane.lane_id for lane in lanes}) == len(lanes)
    assert len({lane.label for lane in lanes}) == len(lanes)
    assert lanes[0].workdir.startswith("/work/fleet/run/")


def test_legacy_roster_cannot_create_a_sixth_specialist() -> None:
    lanes = SwarmManager._fleet_roster(_manager(), "run", 9, None)
    # Two models, nine requested per model -> clamped to one squad each.
    assert len(lanes) == 2 * FLEET_GROUP_SIZE
    for lane in lanes:
        assert lane.category in FLEET_GROUP_ROLES
    first_model = [lane.category for lane in lanes[:FLEET_GROUP_SIZE]]
    assert first_model == list(FLEET_GROUP_ROLES)


def test_there_is_no_analyst_specialist() -> None:
    assert "analyst" not in FLEET_ROLE_ORDER
    assert "analyst" not in AGENT_META
    assert "analyst" not in AGENT_TOOLS
    assert DELEGATABLE == FLEET_GROUP_ROLES
    assert set(AGENT_META) - {"captain"} == set(FLEET_GROUP_ROLES)


def test_api_rejects_a_sixth_agent_or_sixth_squad() -> None:
    with pytest.raises(ValidationError):
        SwarmStartIn(objective="assess the lab", lanes=6)
    with pytest.raises(ValidationError):
        SwarmStartIn(objective="assess the lab", groups=6)
    with pytest.raises(ValidationError):
        SwarmStartIn(objective="assess the lab", agents=6)
    with pytest.raises(ValidationError):
        SwarmStartIn(objective="assess the lab", rounds=51)
    with pytest.raises(ValidationError):
        ChatRequest(message="assess the lab", groups=6)
    with pytest.raises(ValidationError):
        ChatRequest(message="assess the lab", agents=6)
    assert SwarmStartIn(objective="assess the lab", groups=5, lanes=5).groups == 5


def test_round_caps_are_consistent() -> None:
    manager = _manager()
    assert SwarmManager._rounds_for(manager, 99, False) == 50
    assert SwarmManager._rounds_for(manager, None, True) == 50
    assert SwarmManager._rounds_for(manager, None, True, True) == 10_000
    assert SwarmManager._rounds_for(manager, None, False) == 2


def test_captain_prompt_describes_squads_not_six_agents() -> None:
    block = swarm_prompt_block()
    assert "squads of five" in block
    assert "25 agents" in block
    assert "six agents" not in block


def test_learning_board_api_is_mounted() -> None:
    from app.main import app

    paths = {getattr(route, "path", "") for route in app.routes}
    expected = {
        "/api/learning",
        "/api/learning/categories",
        "/api/learning/stats",
        "/api/learning/recommend",
        "/api/learning/{note_id}",
        "/api/learning/{note_id}/related",
        "/api/learning/{note_id}/versions",
        "/api/learning/{note_id}/usage",
        "/api/learning/{note_id}/review",
        "/api/memory",
        "/api/memory/digest",
    }
    assert expected <= paths
