import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS_ROOT = REPOSITORY_ROOT / ".github" / "workflows"
E2E_WORKFLOW_PATH = WORKFLOWS_ROOT / "e2e.yml"
DOCKER_SMOKE_WORKFLOW_PATH = WORKFLOWS_ROOT / "docker-smoke.yml"


def test_repository_workflows_do_not_target_self_hosted_runners():
    workflow_files = sorted(WORKFLOWS_ROOT.glob("*.yml"))

    assert workflow_files
    for path in workflow_files:
        workflow = path.read_text(encoding="utf-8")
        assert "self-hosted" not in workflow, path
        assert "itambox-ci" not in workflow, path


def test_e2e_uses_a_hosted_postgresql_service():
    workflow = E2E_WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "runs-on: ubuntu-latest" in workflow
    assert "services:" in workflow
    assert re.search(r"(?m)^\s*image:\s+postgres:18@sha256:[0-9a-f]{64}\s*$", workflow)
    assert "5432:5432" in workflow
    assert "pg_isready" in workflow
    assert "Start PostgreSQL in the runner network namespace" not in workflow
    assert "Remove PostgreSQL" not in workflow


def test_docker_smoke_jobs_use_hosted_docker():
    workflow = DOCKER_SMOKE_WORKFLOW_PATH.read_text(encoding="utf-8")

    assert workflow.count("runs-on: ubuntu-latest") == 2
    assert "name=rootless" not in workflow
    assert "docker version" in workflow
    assert "docker compose version" in workflow
    assert "./scripts/docker-smoke-test.sh" in workflow
