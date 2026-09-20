import hashlib
import os

# 必须在导入 app.main / app.config 之前：让 lifespan 的建表走 sqlite，
# 不依赖真实 Postgres；API 实际使用下方 get_db 覆盖的 StaticPool 内存库。
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import create_access_token
from app.cqrs import (
    METRIC_DUPLICATE_POLICY,
    ConflictError,
    DuplicateMetricError,
    list_events,
    record_metric,
    rebuild_projection_from_events,
    start_run,
)
from app.database import Base, get_db
from app.main import app
from app.models import EventStore, RunProjection


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.compiler import compiles

    @compiles(JSONB, "sqlite")
    def _compile_jsonb_sqlite(_type, compiler, **kw):
        return "JSON"

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def start_one(db):
    return start_run(
        db,
        actor="researcher",
        project="p1",
        name="n1",
        dataset_content_sha256=sha("ds"),
        code_commit_sha="abc1234",
        description=None,
    )


def test_duplicate_policy_is_hardcoded_reject():
    assert METRIC_DUPLICATE_POLICY == "reject"


def test_duplicate_metric_name_is_rejected(db):
    run = start_one(db)
    record_metric(
        db,
        run_id=run.id,
        actor="researcher",
        name="acc",
        value=0.9,
        step=1,
        expected_version=1,
    )

    # 第二次同名指标：拒绝，且不以新值覆盖
    with pytest.raises(DuplicateMetricError) as exc_info:
        record_metric(
            db,
            run_id=run.id,
            actor="researcher",
            name="acc",
            value=0.95,
            step=2,
            expected_version=2,
        )
    assert exc_info.value.status_code == 409
    assert "reject" in exc_info.value.message

    proj = db.get(RunProjection, run.id)
    # 投影只保留首次记录：1 条指标、原值 0.9、version 未前进
    assert proj.version == 2
    assert len(proj.metrics_json) == 1
    assert proj.metrics_json[0]["name"] == "acc"
    assert proj.metrics_json[0]["value"] == 0.9


def test_rejected_submit_appends_no_event_but_history_remains(db):
    run = start_one(db)
    record_metric(
        db,
        run_id=run.id,
        actor="researcher",
        name="loss",
        value=1.0,
        step=1,
        expected_version=1,
    )
    try:
        record_metric(
            db,
            run_id=run.id,
            actor="researcher",
            name="loss",
            value=0.5,
            step=2,
            expected_version=2,
        )
    except ConflictError:
        pass

    events = list_events(db, run.id)
    metric_events = [e for e in events if e.event_type == "MetricRecorded"]
    # 首次 MetricRecorded 仍可追溯；被拒绝的第二次没有写入任何事件
    assert [e.event_type for e in events] == ["RunStarted", "MetricRecorded"]
    assert len(metric_events) == 1
    assert metric_events[0].payload_json["value"] == 1.0

    # 事件重放与投影一致
    rebuilt = rebuild_projection_from_events(db, run.id)
    stored = db.get(RunProjection, run.id)
    assert rebuilt.version == stored.version
    assert len(rebuilt.metrics_json) == len(stored.metrics_json) == 1


def test_distinct_metric_names_still_allowed(db):
    run = start_one(db)
    run = record_metric(
        db,
        run_id=run.id,
        actor="researcher",
        name="acc",
        value=0.9,
        step=1,
        expected_version=1,
    )
    run = record_metric(
        db,
        run_id=run.id,
        actor="researcher",
        name="loss",
        value=0.3,
        step=1,
        expected_version=run.version,
    )
    assert run.version == 3
    assert [m["name"] for m in run.metrics_json] == ["acc", "loss"]


@pytest.fixture()
def client(db):
    def _override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def auth_headers(username: str, role: str) -> dict:
    token = create_access_token(username, role)
    return {"Authorization": f"Bearer {token}"}


def test_api_auditor_cannot_record_metric(client, db):
    run = start_one(db)
    resp = client.post(
        f"/api/runs/{run.id}/metrics",
        json={"name": "acc", "value": 0.9, "step": 1, "expected_version": 1},
        headers=auth_headers("auditor", "auditor"),
    )
    assert resp.status_code == 403
    # 被拒绝后没有事件、投影无指标
    stored = db.get(RunProjection, run.id)
    assert stored.version == 1
    assert stored.metrics_json == []
    assert (
        db.scalars(
            select(EventStore).where(EventStore.event_type == "MetricRecorded")
        ).all()
        == []
    )


def test_api_duplicate_metric_returns_409_with_message(client, db):
    run = start_one(db)
    first = client.post(
        f"/api/runs/{run.id}/metrics",
        json={"name": "acc", "value": 0.9, "step": 1, "expected_version": 1},
        headers=auth_headers("researcher", "researcher"),
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/runs/{run.id}/metrics",
        json={"name": "acc", "value": 0.95, "step": 2, "expected_version": 2},
        headers=auth_headers("researcher", "researcher"),
    )
    assert second.status_code == 409
    assert "acc" in second.json()["detail"]
    assert "reject" in second.json()["detail"]

    # 指标区与策略一致：只保留首次值
    detail = client.get(
        f"/api/runs/{run.id}", headers=auth_headers("researcher", "researcher")
    ).json()
    assert len(detail["metrics_json"]) == 1
    assert detail["metrics_json"][0]["value"] == 0.9


def test_metric_policy_endpoint(client):
    resp = client.get(
        "/api/policies/metrics", headers=auth_headers("auditor", "auditor")
    )
    assert resp.status_code == 200
    assert resp.json()["duplicate_name"] == "reject"
