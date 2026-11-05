from pathlib import Path

from moni_token import task


def test_default_runs_on_battery():
    s = task.install_script(Path(r"C:\x y\pythonw.exe"))
    assert "-AllowStartIfOnBatteries" in s and "-DontStopIfGoingOnBatteries" in s
    assert "-RepetitionInterval (New-TimeSpan -Minutes 2)" in s
    assert "-Force" in s                                   # re-running replaces the task
    assert "'C:\\x y\\pythonw.exe'" in s                   # quoted path with a space
    assert "-LogonType Interactive" in s


def test_no_battery_keeps_task_scheduler_restriction():
    s = task.install_script(Path("pythonw.exe"), interval_min=5, battery=False)
    assert "Batter" not in s
    assert "-Minutes 5)" in s


def test_map_task_starts_at_logon_and_keeps_running():
    s = task.map_script(Path("pythonw.exe"), port=5000)
    assert "-AtLogOn" in s and "-m moni_token.cli map --port 5000" in s
    assert "[TimeSpan]::Zero" in s and "-RestartCount 3" in s      # no time limit, restarted on crash
    assert "Start-ScheduledTask" in s and "-Force" in s
    assert "-AllowStartIfOnBatteries" in s
    assert "Batter" not in task.map_script(Path("pythonw.exe"), battery=False)


def test_uninstall_removes_both_tasks():
    s = task.uninstall_script()
    assert "'moni_token check'" in s and "'moni_token map'" in s


def test_quotes_are_escaped():
    assert task._ps_quote("a'b") == "'a''b'"


def test_runs_only_local_powershell_scheduler_cmdlets(monkeypatch):
    calls = []
    monkeypatch.setattr(task.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0})())
    task.run_ps(task.install_script(Path("pythonw.exe")))
    cmd = calls[0]
    assert cmd[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    body = cmd[4].lower()
    assert "scheduledtask" in body and "claude" not in body and "http" not in body


def test_non_windows_is_refused(monkeypatch, capsys):
    monkeypatch.setattr(task.sys, "platform", "linux")
    assert task.main_install(2, True) == 2
    assert "Windows" in capsys.readouterr().out
