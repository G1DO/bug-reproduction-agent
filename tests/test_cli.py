import subprocess


def test_command_runs() -> None:
    result = subprocess.run(["bugrepro"], capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert "sandbox" in result.stdout
