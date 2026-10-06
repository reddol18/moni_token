"""Desktop notification. The only module allowed to start a process (an OS notifier — never `claude`).

Ported from moni_pod (same author): on Windows the toast must run synchronously in the calling process;
a detached child was killed with the parent's process tree and the toast never appeared.
"""
import os
import subprocess
import sys

WINDOWS_POWERSHELL_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_PS = ("$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
       "ContentType = WindowsRuntime]; "
       "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
       "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
       "$t = $x.GetElementsByTagName('text'); "
       "$null = $t.Item(0).AppendChild($x.CreateTextNode($env:MONI_NOTIFY_TITLE)); "
       "$null = $t.Item(1).AppendChild($x.CreateTextNode($env:MONI_NOTIFY_BODY)); "
       "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
       f"'{WINDOWS_POWERSHELL_AUMID}').Show([Windows.UI.Notifications.ToastNotification]::new($x))")


def command() -> list[str]:
    if sys.platform == "win32":
        return ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", _PS]
    if sys.platform == "darwin":
        return ["osascript", "-e", 'display notification (system attribute "MONI_NOTIFY_BODY") '
                                   'with title (system attribute "MONI_NOTIFY_TITLE")']
    return ["sh", "-c", 'notify-send "$MONI_NOTIFY_TITLE" "$MONI_NOTIFY_BODY"']


def desktop_notify(title: str, body: str, run=subprocess.run, timeout: float = 4.0) -> bool:
    # text goes through env vars, never interpolated into the script
    env = dict(os.environ, MONI_NOTIFY_TITLE=title[:80], MONI_NOTIFY_BODY=body[:240])
    try:
        r = run(command(), env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                creationflags=0x08000000 if sys.platform == "win32" else 0)  # CREATE_NO_WINDOW
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
