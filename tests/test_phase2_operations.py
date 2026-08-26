from pathlib import Path


def test_runtime_validates_preprovisioned_bucket_and_never_creates_per_artifact():
    station = Path("scripts/run_station.py").read_text("utf-8")
    worker = Path("scripts/run_worker.py").read_text("utf-8")
    dispatcher = Path("src/label_inspection/station/dispatcher.py").read_text("utf-8")
    inference = Path(
        "src/label_inspection/worker/inference_worker.py"
    ).read_text("utf-8")

    assert "store.validate_bucket(config.artifact_bucket)" in station
    assert "store.validate_bucket(config.artifact_bucket)" in worker
    assert "store.ensure_bucket" not in dispatcher
    assert "store.ensure_bucket" not in inference


def test_worker_runtime_and_tests_share_retry_message_handler_core():
    runtime = Path("scripts/run_worker.py").read_text("utf-8")
    worker_source = Path(
        "src/label_inspection/worker/inference_worker.py"
    ).read_text("utf-8")
    worker_exports = Path("src/label_inspection/worker/__init__.py").read_text("utf-8")

    assert "RetryingWorkerMessageHandler" in runtime
    assert "class WorkerMessageHandler" not in worker_source
    assert '"WorkerMessageHandler"' not in worker_exports


def test_worker_has_explicit_systemd_restart_contract_for_broker_loss():
    unit = Path("ops/systemd/vision-inference-worker.service").read_text("utf-8")
    runbook = Path("tasks/phase2_operations_runbook.md").read_text("utf-8")
    runtime = Path("scripts/run_worker.py").read_text("utf-8")

    assert "Restart=on-failure" in unit
    assert "ExecStart=" in unit and "scripts/run_worker.py" in unit
    assert "BROKER_CONNECTION_LOST" in runtime
    assert "systemd" in runbook.lower()
    assert "exits" in runbook.lower()


def test_phase2_dependency_requirements_pin_transport_versions():
    project = Path("pyproject.toml").read_text("utf-8")

    assert '"minio==7.2.20"' in project
    assert '"pika==1.4.4"' in project
