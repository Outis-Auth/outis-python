import py_compile
from pathlib import Path

import pytest

from outis.__main__ import main
from outis._templates import RUNTIMES, render

EXAMPLES = Path(__file__).parent.parent / "examples"
EXAMPLE_FOR = {"plain": "background_worker.py", "fastapi": "fastapi_webhook.py", "celery": "celery_task.py"}


@pytest.mark.parametrize("runtime", sorted(RUNTIMES))
def test_init_worker_writes_a_starter_that_compiles(runtime, tmp_path, capsys):
    assert main(["init", "worker", "--runtime", runtime, "--dir", str(tmp_path)]) == 0
    written = sorted(p for p in tmp_path.rglob("*.py"))
    assert [p.relative_to(tmp_path).as_posix() for p in written] == sorted(RUNTIMES[runtime])
    for path in written:
        py_compile.compile(str(path), cfile=str(path.with_suffix(".pyc")), doraise=True)
        assert "__MODULE__" not in path.read_text()
    assert "wrote" in capsys.readouterr().out


def test_init_worker_refuses_to_overwrite(tmp_path, capsys):
    target = tmp_path / "outis_worker.py"
    target.write_text("mine")
    assert main(["init", "worker", "--dir", str(tmp_path)]) == 1
    assert target.read_text() == "mine"
    assert "refusing to overwrite" in capsys.readouterr().err


def test_keygen_prints_a_usable_key(capsys):
    from outis import _intent

    assert main(["keygen"]) == 0
    assert len(_intent.load_key(capsys.readouterr().out.strip())) == 32


def test_examples_compile(tmp_path):
    files = sorted(EXAMPLES.rglob("*.py"))
    assert len(files) == 9
    for i, path in enumerate(files):
        py_compile.compile(str(path), cfile=str(tmp_path / f"{i}.pyc"), doraise=True)


def test_examples_are_the_scaffolder_output():
    for runtime, name in EXAMPLE_FOR.items():
        path = EXAMPLES / name
        assert path.read_text() == render(RUNTIMES[runtime]["outis_worker.py"], path.stem), name
    for rel, content in RUNTIMES["temporal"].items():
        path = EXAMPLES / "temporal" / Path(rel).name
        assert path.read_text() == render(content, path.stem), rel
