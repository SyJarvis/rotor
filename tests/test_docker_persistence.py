from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_docker_image_persists_database_and_conversations_under_data():
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "DATABASE_URL=sqlite+aiosqlite:////data/rotor.db" in dockerfile
    assert "CONVERSATION_STORE_DIR=/data/conversations" in dockerfile
    assert 'VOLUME ["/data"]' in dockerfile


def test_compose_persists_conversations_under_data():
    compose = (ROOT / "docker-compose.yml").read_text()

    assert "CONVERSATION_STORE_DIR=/data/conversations" in compose
    assert "./data:/data" in compose
