from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "infra" / "phase2" / "docker-compose.yml"
ENV_EXAMPLE = ROOT / "infra" / "phase2" / ".env.example"
BOOTSTRAP = ROOT / "infra" / "phase2" / "minio" / "bootstrap.sh"


def test_phase2_compose_contains_persistent_minio_rabbit_and_bootstrap():
    text = COMPOSE.read_text("utf-8")

    assert "minio:" in text
    assert "minio-bootstrap:" in text
    assert "rabbitmq:" in text
    assert "minio-data:/data" in text
    assert "rabbitmq-data:/var/lib/rabbitmq" in text
    assert "rabbitmq-diagnostics" in text
    assert "./minio/bootstrap.sh:/opt/bootstrap.sh:ro" in text
    assert "mediamtx" not in text.lower()


def test_phase2_env_example_connects_application_to_local_services_without_full_roi():
    text = ENV_EXAMPLE.read_text("utf-8")

    assert "VISION_MINIO_ENDPOINT=127.0.0.1:9000" in text
    assert "VISION_RABBITMQ_URL=amqp://vision:" in text
    assert "VISION_RTSP_URL=http://10.10.12.13:8080/video" in text
    assert "VISION_LABEL_ROI=REPLACE_WITH_CALIBRATED" in text
    assert "VISION_LABEL_ROI=0,0,1,1" not in text


def test_minio_bootstrap_creates_restricted_app_policy_without_delete():
    text = BOOTSTRAP.read_text("utf-8")

    assert "mc mb --ignore-existing" in text
    assert "mc admin policy create" in text
    assert "mc admin user add" in text
    assert '"s3:GetObject"' in text
    assert '"s3:PutObject"' in text
    assert '"s3:DeleteObject"' not in text
