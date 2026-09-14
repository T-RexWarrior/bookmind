"""Phase 6 API tests — long-term Recovery endpoints over HTTP."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from bookmind.api.app import create_app
from bookmind.domain.enums import Level, LevelStatus
from bookmind.domain.models import LevelRecord


@pytest.fixture()
def client():
    import bookmind.api.app as appmod
    appmod._REPO = type(appmod._REPO)()
    appmod._QA_SERVICE = None
    appmod._MAPPING_SERVICE = None
    appmod._LAST_MAPPING = {}
    return TestClient(create_app())


def _setup(client):
    client.post("/users", json={"user_id": "u1"})
    client.post("/projects", json={"project_id": "p1", "learner_id": "u1", "name": "Java"})
    client.post("/projects/p1/books/seed", json={"book_id": "b1"})


def _expire_concept(client, concept_id="c_polymorphism", days_ago=60.0):
    """Drive a high-value concept into EXPIRED by writing a VERIFIED record
    with an old timestamp, then reading state (which derives EXPIRED)."""
    import bookmind.api.app as appmod
    repo = appmod._REPO
    state = repo.get_state("p1", concept_id)
    state.set_level_record(Level.L2, LevelRecord(
        status=LevelStatus.VERIFIED,
        verified_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        stability_days=2.0,
    ))
    state.current_verified_level = Level.L2
    state.highest_ever_level = Level.L2
    repo.save_state(state)


def test_recovery_plan_endpoint_returns_candidates(client):
    _setup(client)
    _expire_concept(client, "c_polymorphism", days_ago=60.0)
    last_active = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    r = client.get("/projects/p1/recovery", params={"last_active_at": last_active})
    assert r.status_code == 200
    body = r.json()
    assert body["project_id"] == "p1"
    assert body["days_away"] >= 59.0
    assert len(body["candidates"]) >= 1
    assert any(c["concept_id"] == "c_polymorphism" for c in body["candidates"])
    assert body["recommendation"] == "recovery_check"


def test_recovery_plan_no_candidates_recommends_continue(client):
    _setup(client)
    # No state written → nothing EXPIRED/UNSTABLE.
    last_active = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    r = client.get("/projects/p1/recovery", params={"last_active_at": last_active})
    assert r.status_code == 200
    body = r.json()
    assert body["recommendation"] == "continue"
    assert body["candidates"] == []


def test_recovery_plan_unknown_project(client):
    _setup(client)
    r = client.get("/projects/NOPE/recovery")
    assert r.status_code == 404


def test_recovery_plan_bad_timestamp(client):
    _setup(client)
    r = client.get("/projects/p1/recovery", params={"last_active_at": "not-a-date"})
    assert r.status_code == 400


def test_recovery_choose_continue_overrides(client):
    _setup(client)
    _expire_concept(client, "c_polymorphism", days_ago=60.0)
    r = client.post("/projects/p1/recovery/choose", json={"choice": "continue"})
    assert r.status_code == 200
    body = r.json()
    assert body["choice"] == "continue"
    assert body["recommended_action"] == "CONTINUE_READING"
    assert body["target_concept_id"] is None


def test_recovery_choose_check_picks_top_candidate(client):
    _setup(client)
    _expire_concept(client, "c_polymorphism", days_ago=60.0)
    r = client.post("/projects/p1/recovery/choose", json={"choice": "recovery_check"})
    assert r.status_code == 200
    body = r.json()
    assert body["recommended_action"] == "VERIFY"
    assert body["target_concept_id"] == "c_polymorphism"


def test_recovery_choose_invalid_choice(client):
    _setup(client)
    r = client.post("/projects/p1/recovery/choose", json={"choice": "bogus"})
    assert r.status_code == 400
