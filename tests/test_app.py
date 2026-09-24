import uuid

import pytest

from app import app, init_db


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Each test gets its own temporary SQLite database.
    import app as application

    monkeypatch.setattr(application, "DB_PATH", tmp_path / "test.db")
    init_db()

    app.config["TESTING"] = True

    with app.test_client() as test_client:
        yield test_client


def payload(goal, role="operator", task_id=None):
    return {
        "task_id": task_id or str(uuid.uuid4()),
        "workspace_id": "demo-a",
        "user_id": "user-7",
        "role": role,
        "goal": goal,
        "context": {},
        "mode": "read_only",
        "limits": {
            "max_steps": 3,
            "max_tool_calls": 1,
            "max_runtime_seconds": 10,
        },
    }


def test_main_scenario(client):
    """Основной сценарий: доступный источник формирует доказательный ответ."""
    response = client.post(
        "/tasks",
        json=payload("What is the data retention period?")
    )

    assert response.status_code == 201

    data = response.get_json()
    assert data["status"] == "COMPLETED"
    assert data["result"]["citations"]


def test_safe_refusal(client):
    """Без основания система не придумывает ответ."""
    response = client.post(
        "/tasks",
        json=payload("What is the server color?")
    )

    assert response.status_code == 201

    data = response.get_json()
    assert data["status"] == "FAILED"
    assert data["result"]["type"] == "refusal"
    assert data["result"]["citations"] == []


def test_duplicate_task(client):
    """Повторный task_id не создаёт вторую задачу."""
    task_id = str(uuid.uuid4())

    first = client.post(
        "/tasks",
        json=payload("What is the data retention period?", task_id)
    )
    second = client.post(
        "/tasks",
        json=payload("What is the data retention period?", task_id)
    )

    assert first.status_code == 201
    assert second.status_code == 409
