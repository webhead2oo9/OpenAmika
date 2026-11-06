"""Files that ship as two identical copies must stay identical (README_IF_YOU_ARE_AI_AGENT.md, rule 7). Offline.

  * brain/po_contract.py and speech/po_contract.py: the output contract both parsers load (the brain and the speech
    layer run in different environments, so each has its own copy). Compared in the working tree.
  * local_guard.py in brain/, speech/, messengers/ and vendor/breeze-bridge/. Compared in the working tree.
  * her persona in brain/ (scenes.json, 例子.txt, 表层.txt, 思考.txt) and its shipped copy in brain/defaults/, which
    Setup → Danger zone → "Reset everything" restores. Compared in git's index: the Setup page edits the working-tree
    copies on purpose (her personality is yours to change), but what is committed must ship them equal.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PERSONA = ("scenes.json", "例子.txt", "表层.txt", "思考.txt")


def text(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")          # git may check either copy out with CRLF


def test_both_output_contracts_are_the_same_file():
    brain, speech = ROOT / "brain" / "po_contract.py", ROOT / "speech" / "po_contract.py"
    assert text(brain) == text(speech), ("brain/po_contract.py and speech/po_contract.py differ: change both "
                                         "(copy one over the other)")


@pytest.mark.parametrize("folder", ("speech", "messengers", "vendor/breeze-bridge"))
def test_every_service_guards_requests_with_the_same_file(folder):
    brain, other = ROOT / "brain" / "local_guard.py", ROOT / folder / "local_guard.py"
    assert text(brain) == text(other), (f"brain/local_guard.py and {folder}/local_guard.py differ: change every copy "
                                        "(copy brain/local_guard.py over the others)")


def staged(rel: str) -> bytes:
    p = subprocess.run(["git", "-c", "core.quotepath=off", "show", f":{rel}"], cwd=ROOT, capture_output=True, timeout=60)
    if p.returncode != 0:
        pytest.skip(f"git cannot show {rel} here ({p.stderr.decode('utf-8', 'replace').strip()[:120]})")
    return p.stdout.replace(b"\r\n", b"\n")


@pytest.mark.parametrize("name", PERSONA)
def test_her_persona_ships_equal_to_its_reset_copy(name):
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    assert staged(f"brain/{name}") == staged(f"brain/defaults/{name}"), (
        f"brain/{name} and brain/defaults/{name} differ in git: a change to her shipped persona goes into both")
