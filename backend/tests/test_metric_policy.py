import hashlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.cqrs import (
    ConflictError,
    MetricNameConflictError,
    list_events,
    record_metric,
    rebuild_projection_from_events,
    start_run,
)
from app.database import Base, get_db
from app.main import app
from app.models import RunProjection


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, compiler, **kw):
    return "JSON"


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _start(db):
    return start_run(
        db,
        actor="researcher",
        project="p1",
        name="n1",
        dataset_content_sha256=sha("ds"),
        code_commit_sha="abc1234",
        description=None,
    )


def test_duplicate_metric_name_is_rejected(db):
    run = _start(db)
    run = record_metric(
        db,
        run_id=run.id,
        actor="researcher",
        name="acc",
        value=0.9,
        step=1,
        expected_version=1,
    )
    assert run.version == 2

    # 第二次记录同名指标：按写死的 reject 策略拒绝
    with pytest.raises(MetricNameConflictError) as exc:
        record_metric(
            db,
            run_id=run.id,
            actor="researcher",
            name="acc",
            value=0.95,
            step=2,
            expected_version=2,
        )
    assert exc.value.status_code == 409
    assert "acc" in exc.value.message

    # 拒绝后：版本不前进、事件不追加、投影仍只有一条同名指标
    proj = db.get(RunProjection, run.id)
    assert proj.version == 2
    assert [m["name"] for m in proj.metrics_json] == ["acc"]
    assert proj.metrics_json[0]["value"] == 0.9
    assert len(list_events(db, run.id)) == 2  # RunStarted + 一次 MetricRecorded

    # 事件重放得到的投影与在线投影一致（每个指标名唯一）
    rebuilt = rebuild_projection_from_events(db, run.id)
    assert [m["name"] for m in rebuilt.metrics_json] == ["acc"]


def test_distinct_metric_names_still_accepted(db):
    run = _start(db)
    run = record_metric(
        db, run_id=run.id, actor="researcher", name="acc", value=0.9,
        step=1, expected_version=1,
    )
    run = record_metric(
        db, run_id=run.id, actor="researcher", name="loss", value=0.1,
        step=2, expected_version=2,
    )
    assert run.version == 3
    assert {m["name"] for m in run.metrics_json} == {"acc", "loss"}


@pytest.fixture()
def client(db):
    """API client backed by the same in-memory engine as the db fixture."""
    engine = db.bind
    Session = sessionmaker(bind=engine)

    def _override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_get_db
    # 预置一条 running run
    run = _start(db)
    # 不作为 context manager 使用，避免触发 lifespan 对真实 Postgres 执行 create_all；
    # 表已由 db fixture 在内存 SQLite 中建好，get_db 也已被覆盖。
    c = TestClient(app)
    yield c, run, engine, Session
    app.dependency_overrides.clear()


def _token(client, username, password):
    resp = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def test_auditor_cannot_record_metric(client):
    c, run, _engine, Session = client
    token = _token(c, "auditor", "audit123456")
    resp = c.post(
        f"/api/runs/{run.id}/metrics",
        json={"name": "acc", "value": 0.9, "step": 1, "expected_version": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    # 审计员被拒后未写入任何 MetricRecorded 事件，投影也无指标
    with Session() as session:
        events = list_events(session, run.id)
        assert [e.event_type for e in events] == ["RunStarted"]
        assert session.get(RunProjection, run.id).metrics_json == []


def test_api_duplicate_metric_returns_409(client):
    c, run, _engine, _Session = client
    token = _token(c, "researcher", "lab123456")
    headers = {"Authorization": f"Bearer {token}"}

    first = c.post(
        f"/api/runs/{run.id}/metrics",
        json={"name": "acc", "value": 0.9, "step": 1, "expected_version": 1},
        headers=headers,
    )
    assert first.status_code == 200, first.text

    dup = c.post(
        f"/api/runs/{run.id}/metrics",
        json={"name": "acc", "value": 0.95, "step": 2, "expected_version": 2},
        headers=headers,
    )
    assert dup.status_code == 409
    assert "acc" in dup.json()["detail"]
