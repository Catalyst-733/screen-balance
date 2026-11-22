# Screen Balance

A small Windows tray app that helps you not overuse your PC.

- **Break reminders**: after 50 minutes of continuous use (adjustable), a popup nudges you to stand up and stretch. Being away for 5 minutes counts as a break and resets the timer.
- **Daily time limit**: gentle warnings at 80% and 100% of your daily limit, then every 30 minutes while you're over it.
- **Per-app tracking**: a dashboard shows today's screen time, your top apps and the last 7 days.
- **Only active time counts**: it stops counting after 2 minutes with no mouse or keyboard input, while the PC is locked, and while it sleeps.

Everything stays on your PC. There's no network access, no account and no telemetry, and it records only app names (no window titles, keystrokes or screenshots).

## Requirements

- Windows 10 or 11
- Python 3.10+ (from [python.org](https://www.python.org/downloads/) or the Microsoft Store)

## Install

```powershell
git clone https://github.com/Catalyst-733/screen-balance.git
cd screen-balance
python -m pip install --user -r requirements.txt
```

## Run

Double-click **`Start Screen Balance.bat`**, or run:

```powershell
pythonw screen_balance.py
```

A ring icon appears in the system tray (click the **^** arrow on the taskbar if you don't see it).

- **Double-click** the icon to open the dashboard
- **Right-click** it for Settings, pausing reminders for an hour, *Start with Windows*, and Quit

To start it automatically when you log in, turn on **Start with Windows** in Settings or the tray menu.

## Settings

| Setting | Default |
|---|---|
| Break reminder after | 50 min |
| Break length (away time that resets the timer) | 5 min |
| Snooze | 10 min |
| Daily limit (0 = off) | 6 h |
| Stop counting after no input for | 120 sec |
| Repeat over-limit warning every | 30 min |

## Data

Settings and usage history are stored in `%USERPROFILE%\.screenbalance\`:

- `config.json`: your settings
- `usage.json`: daily totals per app, keeping the last 60 days
- `error.log`: only written if something goes wrong

To reset everything, quit the app and delete that folder.

## Known limitations

- Watching a video without touching the mouse or keyboard counts as idle. Raise *Idle after* in Settings if you want that time to count.
- Popups appear on the primary monitor.

## Project layout

- `screen_balance.py`: tracking, reminders, tray menu and windows
- `design.py`: drawing for the dashboard, popups and tray icon (Pillow, rendered at 2× for smooth edges)

## License

MIT, see [LICENSE](LICENSE).
