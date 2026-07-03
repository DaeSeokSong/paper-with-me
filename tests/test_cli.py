import shutil
from pathlib import Path

from pwc.cli import main

FIXTURES = Path(__file__).parent / "fixtures"


def _copy_fixtures(data_dir: Path) -> None:
    raw = data_dir / "raw"
    raw.mkdir(parents=True)
    names = {
        "papers.json": "papers.json",
        "links.json": "links.json",
        "evaluation-tables.json": "evaluations.json",
        "methods.json": "methods.json",
        "datasets.json": "datasets.json",
    }
    for src, dst in names.items():
        shutil.copy(FIXTURES / src, raw / dst)


def test_data_dir_after_subcommand(tmp_path, capsys):
    """워크플로가 쓰는 `pwc build --data-dir X` 형태의 인자 순서가 동작해야 한다."""
    _copy_fixtures(tmp_path)
    assert main(["ingest", "--data-dir", str(tmp_path)]) == 0
    assert main(["stats", "--data-dir", str(tmp_path)]) == 0
    assert "papers" in capsys.readouterr().out


def test_data_dir_before_subcommand(tmp_path, capsys):
    _copy_fixtures(tmp_path)
    assert main(["--data-dir", str(tmp_path), "ingest"]) == 0
    assert main(["--data-dir", str(tmp_path), "stats"]) == 0


def test_stats_without_db_fails_cleanly(tmp_path):
    assert main(["stats", "--data-dir", str(tmp_path)]) == 1


def test_ingest_without_dumps_fails_cleanly(tmp_path):
    assert main(["ingest", "--data-dir", str(tmp_path)]) == 1
