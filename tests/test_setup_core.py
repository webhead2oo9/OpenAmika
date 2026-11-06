"""scripts/setup_core.py: the decisions it makes and the files it writes, without installing anything. Offline."""
import contextlib
import importlib.util
import io
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("setup_core", ROOT / "scripts" / "setup_core.py")
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)


@pytest.fixture()
def checkout(tmp_path, monkeypatch):
    """setup_core.py looking at an empty checkout (the shipped templates only)."""
    for rel in ("settings.example.json", ".env.example"):
        shutil.copyfile(ROOT / rel, tmp_path / rel)
    (tmp_path / "brain").mkdir()
    monkeypatch.setattr(core, "ROOT", tmp_path)
    monkeypatch.setattr(core, "ENVS", tmp_path / "envs")
    core.ROWS.clear()
    return tmp_path


def rows():
    return {name: (mark.strip(), detail) for mark, name, detail in core.ROWS}


def test_gpu_line_parsing():
    assert core.parse_gpu("NVIDIA GeForce RTX 5080, 16303\n") == ("NVIDIA GeForce RTX 5080", 16303)
    assert core.parse_gpu("NVIDIA GeForce RTX 3060, 12288\nNVIDIA GeForce GT 710, 2048\n") == ("NVIDIA GeForce RTX 3060", 12288)
    assert core.parse_gpu("") is None and core.parse_gpu("No devices were found") is None


def test_her_eyes_need_a_16_gb_card_unless_asked():
    big, small = ("RTX 5080", 16303), ("RTX 4070", 12282)
    assert core.vision_wanted("auto", "cuda", big)[0] is True
    on, why = core.vision_wanted("auto", "cuda", small)
    assert on is False and "--vision on" in why and "12 GB" in why
    assert core.vision_wanted("auto", "cpu", None)[0] is False
    assert core.vision_wanted("on", "cuda", small)[0] is True
    assert core.vision_wanted("off", "cuda", big) == (False, "skipped (--vision off)")


def test_settings_point_at_the_new_environments(checkout):
    cfg = core.settings_from_example("cuda", vision_on=True)
    venv = ["Scripts", "python.exe"] if core.IS_WIN else ["bin", "python"]
    assert Path(cfg["brain_python"]).parts == ("envs", "brain", *venv)
    assert Path(cfg["embedder"]["python"]).parts == ("envs", "embed", *venv)
    assert cfg["vision"]["enabled"] is True and cfg["speech_device"] == "cuda:0"
    cpu = core.settings_from_example("cpu", vision_on=False)
    assert cpu["speech_device"] == "cpu" and cpu["embedder"]["device"] == "cpu" and cpu["embedder"]["dtype"] == "fp32"
    assert cpu["vision"]["enabled"] is False
    assert cpu["services"]["tts"]["command"][0].endswith("python.exe")        # her voice server block is kept


def test_a_conda_environment_from_the_old_readme_is_used_as_it_is(checkout):
    if not core.IS_WIN:
        pytest.skip("conda -p layout is a Windows path")
    (checkout / "envs" / "brain").mkdir(parents=True)
    (checkout / "envs" / "brain" / "python.exe").write_bytes(b"")
    assert core.env_python(checkout / "envs" / "brain") == checkout / "envs" / "brain" / "python.exe"
    assert core.settings_from_example("cuda", False)["brain_python"] == str(Path("envs/brain/python.exe"))


def make_envs(checkout, *names):
    for name in names:
        exe = core.env_python(checkout / "envs" / name)
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"")


def test_settings_json_is_created_once_and_never_changed(checkout):
    args = core.argparse.Namespace(check=False)
    core.ensure_settings("cuda", True, args)
    path = checkout / "settings.json"
    assert json.loads(path.read_text("utf-8"))["vision"]["enabled"] is True
    assert rows()["settings.json"][0] == "OK" and "created" in rows()["settings.json"][1]
    core.ROWS.clear()
    core.ensure_settings("cuda", True, args)          # its environments are not made yet: their own rows say why
    mark, detail = rows()["settings.json"]
    assert mark == "--" and "not made yet" in detail and "see the speech recognition row" in detail
    make_envs(checkout, "brain", "speech", "embed", "vision")
    before = path.read_bytes()
    core.ROWS.clear()
    core.ensure_settings("cpu", True, args)                                   # other choices: the file stays as it is
    assert path.read_bytes() == before and rows()["settings.json"] == ("OK", "existing, not changed (its interpreters exist)")


def test_an_interpreter_outside_envs_that_is_gone_names_the_environment_to_use(checkout):
    cfg = core.settings_from_example("cuda", False)
    cfg["speech_python"] = str(checkout / "elsewhere" / "python.exe")
    (checkout / "settings.json").write_text(json.dumps(cfg), "utf-8")
    make_envs(checkout, "brain", "speech", "embed")
    with pytest.raises(core.SetupError) as caught:
        core.ensure_settings("cuda", False, core.argparse.Namespace(check=False))
    message = str(caught.value)
    assert "as speech_python, which does not exist" in message
    assert f"point speech_python at {core.rel(core.env_python(checkout / 'envs' / 'speech'))}" in message


def test_a_rerun_after_an_interrupted_first_run_ends_with_ok_settings(checkout, monkeypatch):
    """The first run made envs\\brain and settings.json, then stopped (Ctrl+C in the speech environment's torch
    download); the re-run makes the rest and its summary has no !! row for settings.json."""
    made = []

    def make_env(key, base_py, device, args):               # stands in for venv + torch + pip
        folder = core.ENV_SPECS[key][0]
        make_envs(checkout, folder)
        made.append(folder)
        exe = core.env_python(core.ENVS / folder)
        core.row("OK ", core.ROW_NAMES[key], f"{core.rel(exe)} (existing)")
        return exe

    monkeypatch.setattr(core, "ensure_env", make_env)
    monkeypatch.setattr(core.base, "find_base_python", lambda explicit: (["py", "-3.11"], (3, 11, 9)))
    monkeypatch.setattr(core, "gpu_info", lambda: ("NVIDIA GeForce RTX 5080", 16303))
    monkeypatch.setattr(core, "ensure_speech_model", lambda args: core.row("OK ", "speech model", "verified"))
    monkeypatch.setattr(core, "ensure_hf_model", lambda label, vpy, folder, repo, *rest: core.row("OK ", label, repo))
    make_envs(checkout, "brain")
    (checkout / "settings.json").write_text(json.dumps(core.settings_from_example("cuda", True)), "utf-8")
    with contextlib.redirect_stdout(io.StringIO()) as out:
        rc = core.main([])
    got = rows()
    assert rc == 0, out.getvalue()
    assert made == ["brain", "speech", "embed", "vision"]
    assert got["settings.json"][0] == "OK" and not any(mark == "!!" for mark, _ in got.values())
    assert got["vision model"] == ("OK", "Qwen/Qwen3-VL-2B-Instruct")
    assert list(got)[-2:] == ["settings.json", "brain/.env"]                 # written after the environments
    assert (".\\Start_OpenAmika.bat" if core.IS_WIN else "launch.py") in out.getvalue()      # PowerShell needs the .\


def test_models_named_in_settings_json_are_the_ones_fetched(checkout):
    assert core.chosen_models() == ("google/embeddinggemma-2", "qwen3-vl-2b")
    cfg = core.settings_from_example("cuda", True)
    cfg["embedder"]["model"], cfg["vision"]["model"] = "intfloat/multilingual-e5-large", "smolvlm2-500m"
    (checkout / "settings.json").write_text(json.dumps(cfg), "utf-8")
    assert core.chosen_models() == ("intfloat/multilingual-e5-large", "smolvlm2-500m")
    assert core.vision_repo("smolvlm2-500m") == "HuggingFaceTB/SmolVLM2-500M-Video-Instruct"
    with pytest.raises(core.SetupError, match="does not know"):
        core.vision_repo("no-such-model")


def test_a_damaged_package_is_installed_again_but_never_torch_or_a_too_long_path():
    found = {"bad": {"cv2": "ImportError: DLL load failed while importing cv2: The specified module could not be found.",
                     "torch": "OSError: [WinError 126] fbgemm.dll", "PIL": "ImportError: cannot import name '_imaging'",
                     "resampy": "ImportError: DLL load failed while importing _box: The filename or extension is too long."},
             "broken": {"cv2": ["opencv-python==4.12.0.88"], "torch": ["torch==2.10.0+cu128"], "PIL": ["pillow==12.0.0"],
                        "resampy": ["resampy==0.4.3"]}}
    assert core.damaged_dists(found) == ["opencv-python==4.12.0.88", "pillow==12.0.0"]
    assert core.damaged_dists({"bad": {"funasr": "ModuleNotFoundError"}, "broken": {}}) == []


def test_brain_env_file_is_copied_and_a_missing_key_is_informational(checkout):
    args = core.argparse.Namespace(check=False)
    core.ensure_env_file(args)
    assert (checkout / "brain" / ".env").read_text("utf-8") == (checkout / ".env.example").read_text("utf-8")
    mark, detail = rows()["brain/.env"]
    assert mark == "--" and "no OpenRouter key yet" in detail and "wizard" in detail
    (checkout / "brain" / ".env").write_text("OPENROUTER_API_KEY=placeholder-for-the-test-1234\n", "utf-8")
    core.ROWS.clear()
    core.ensure_env_file(args)
    assert rows()["brain/.env"] == ("OK", "OpenRouter key set (...1234)")


def test_onedrive_detection_uses_windows_own_variables(tmp_path, monkeypatch):
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        monkeypatch.delenv(var, raising=False)
    named_like_it = tmp_path / "C--Users-x-OneDrive-Desktop" / "OpenAmika"
    named_like_it.mkdir(parents=True)
    assert core.synced_by_onedrive(named_like_it) is False                    # the word alone is not a sync
    monkeypatch.setenv("OneDrive", str(tmp_path / "C--Users-x-OneDrive-Desktop"))
    assert core.synced_by_onedrive(named_like_it) is True


def test_check_on_an_empty_checkout_lists_everything_and_changes_nothing(checkout, monkeypatch):
    monkeypatch.setattr(core, "gpu_info", lambda: ("NVIDIA GeForce RTX 5080", 16303))
    monkeypatch.setattr(core, "hf_cached", lambda repo: None)
    monkeypatch.setattr(core, "ensure_speech_model", lambda args: (_ for _ in ()).throw(
        core.SetupError("speech/models/ is not complete: run this script without --check")))
    with contextlib.redirect_stdout(io.StringIO()) as out:
        rc = core.main(["--check"])
    got = rows()
    assert rc == 1
    assert got["brain"][0] == "!!" and "not created yet" in got["brain"][1]
    assert got["settings.json"][0] == "!!" and got["speech model"][0] == "!!"
    assert got["memory model"][0] == "--" and "skipped: needs memory (embedder)" in got["memory model"][1]
    assert got["vision (her eyes)"] == ("--", "not installed yet: this script installs them on this GPU")
    assert "Not ready: run this script without --check" in out.getvalue()
    assert sorted(p.name for p in checkout.iterdir()) == [".env.example", "brain", "settings.example.json"]


def test_the_windows_entry_point_parses_and_needs_no_bom():
    ps1 = ROOT / "scripts" / "setup_windows.ps1"
    raw = ps1.read_bytes()
    assert raw.isascii(), "non-ASCII text in a .ps1 needs a UTF-8 BOM for Windows PowerShell 5.1"
    assert b"setup_core.py" in raw and b"@args" in raw
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        pytest.skip("no PowerShell here")
    script = ("$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile($env:OA_PS1, [ref]$null, [ref]$e); "
              "if ($e.Count) { $e | ForEach-Object { $_.Message }; exit 1 }")
    import os
    import subprocess
    p = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True,
                       env={**os.environ, "OA_PS1": str(ps1)}, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
