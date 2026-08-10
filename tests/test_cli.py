from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from conftest import create_test_seq
from seqcomp import __version__
from seqcomp.cli import main


def test_cli_version(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    assert f"seqcomp {__version__}" in capsys.readouterr().out


def test_runtime_version_matches_project_metadata() -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    metadata = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert __version__ == metadata["project"]["version"] == "0.1.0"


@pytest.mark.parametrize(
    "arguments",
    [
        ["compress", "input", "--crf", "52"],
        ["compress", "input", "--crf", "0"],
        ["compress", "input", "--keyint", "0"],
        ["compress", "input", "--codec", "vp9"],
    ],
)
def test_cli_rejects_invalid_codec_parameters(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(arguments)
    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    "arguments",
    [
        ["status", "input", "--ratio", "0"],
        ["status", "input", "--time-ratio", "0"],
    ],
)
def test_cli_rejects_invalid_status_estimates(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(arguments)
    assert exc_info.value.code == 2


def test_python_module_entrypoint() -> None:
    environment = os.environ.copy()
    source_root = str(Path(__file__).parents[1] / "src")
    environment["PYTHONPATH"] = source_root
    result = subprocess.run(
        [sys.executable, "-m", "seqcomp", "--version"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.stdout.strip() == f"seqcomp {__version__}"


def test_cli_status_is_human_readable_and_aggregated(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "source"
    create_test_seq(source)
    code = main(["status", str(source)])
    assert code == 0
    output = capsys.readouterr().out
    assert output.startswith("Status:")
    assert "20x compression" in output
    assert "[raw-only] test-recording.000.seq" in output
    assert "Summary" in output
    assert "GB" in output
    assert "estimated encoding:" in output


@pytest.mark.integration
def test_cli_compresses_a_folder(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "backup"
    create_test_seq(source)
    code = main(["compress", str(source), "--dest", str(destination), "--yes", "--quiet"])
    assert code == 0
    assert (destination / "test-recording.000.mkv").is_file()
    captured = capsys.readouterr()
    assert "Done: compressed 1" in captured.out
    assert "Data:" in captured.out and "GB" in captured.out
    assert "Encoding:" in captured.out and "x video time" in captured.out
    assert captured.err == ""
