import os
import subprocess
import sys
import time
from pathlib import Path

from app.config import settings

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_settings_isolated_from_dotenv() -> None:
    assert settings.storage_backend == "local"
    assert settings.queue_backend == "memory"
    assert settings.database_url == os.environ.get("DATABASE_URL")
    assert "distributed_file_service.db" not in settings.database_url
    assert Path(settings.storage_root).resolve() != (REPO_ROOT / "data").resolve()
    # R2_BUCKET is only ever set by a developer .env; it must not leak into tests.
    assert settings.r2_bucket == ""


def test_env_file_can_be_disabled(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("STORAGE_BACKEND=r2\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "STORAGE_BACKEND"}
    env["DFS_ENV_FILE"] = ""
    env["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", "from app.config import settings; print(settings.storage_backend)"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "local"


def test_slow_storage_fixture_delays_writes(slow_storage) -> None:
    from app.storage import storage

    slow_storage(0.2)
    started = time.perf_counter()
    storage.write_chunk("fixture-check", 0, b"x")
    assert time.perf_counter() - started >= 0.2
