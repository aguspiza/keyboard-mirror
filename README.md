# KeyMirror

Display your PC keystrokes in real time on any browser — phone, tablet, or a second screen. Useful for live coding demos, presentations, and pair programming.

## How it works

```
PC (pynput captures keys)
    └── aiohttp WebSocket server  ──ws://──►  Browser (index.html)
```

- `server.py` listens on port **8765** by default, serves `index.html` over HTTP, and opens a WebSocket endpoint at `/ws`.
- `pynput` intercepts every keystroke globally and forwards it to all connected clients.
- The browser renders the last N characters in a large monospace font with a blinking cursor.

## Requirements

- Python 3.12+
- A phone/tablet on the **same local network** as your PC

## Installation

```bash
cd keymirror
python -m venv .venv
.venv\Scripts\activate      # Windows
pip install -r requirements.txt
```

## Usage

```bash
python server.py [--port 8765] [--host 0.0.0.0]
```

The server prints the URL to open on your phone:

```
==================================================
  KeyMirror Server
==================================================
  Open on phone: http://192.168.1.42:8765
  WebSocket port: 8765
  Start typing on your PC!
  Press Ctrl+C to stop
==================================================
```

Open that URL on any browser on the same network and start typing.

The page auto-reconnects if the connection drops.

## Browser settings

Open the ⚙ gear icon on the page to adjust:

| Setting     | Range     | Default | Description                        |
|-------------|-----------|---------|------------------------------------|
| Buffer size | 5 – 30    | 15      | Number of recent characters shown  |
| Font size   | 24 – 72px | 42px    | Display text size                  |

Settings are saved to `localStorage` and persist across sessions.

## Wire protocol

The server sends plain UTF-8 text over the WebSocket. Special control messages use null-byte delimiters:

| Message         | Effect                          |
|-----------------|---------------------------------|
| `\x00CLEAR\x00` | Clears the display              |
| `\x00PING\x00`  | Server keepalive (ignored)      |
| `\x00LEFT\x00`  | Move cursor left one character  |
| `\x00RIGHT\x00` | Move cursor right one character |
| `\x00UP\x00`    | Move cursor to previous line    |
| `\x00DOWN\x00`  | Move cursor to next line        |
| `\b`            | Backspace                       |
| `\n`            | Newline                         |
| `\t`            | Tab (rendered as 4 spaces)      |
| Any other text  | Appended to the display buffer  |

The server sends a `PING` every 5 seconds of keyboard inactivity to prevent mobile browsers from dropping the idle connection. aiohttp's built-in WebSocket heartbeat runs every 25 seconds as an additional layer.

## Accent / composed character support

Dead keys are handled server-side: pressing `´` followed by `a` sends `á` as a single character. Supported combining accents: acute (`´`), grave (`` ` ``), circumflex (`^`), tilde (`~`).

