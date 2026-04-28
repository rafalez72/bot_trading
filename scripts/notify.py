"""Manda un mensaje a Telegram sin depender de que el bot esté corriendo.

Lee TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID del .env (cwd o paths típicos)
y manda el texto pasado como argumento.

Uso:
    python scripts/notify.py "🔄 Pull de cambios detectado, reiniciando..."
    python scripts/notify.py --silent "Bot reiniciado OK"

Diseñado para ser llamado desde update_and_restart.bat antes/después del
restart de containers (cuando el bot del .py está caído).
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path


def _load_env() -> dict[str, str]:
    """Lee .env del cwd o del directorio del script. No usa python-dotenv para no requerir deps."""
    env: dict[str, str] = {}
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parent.parent / ".env",
    ]
    for path in candidates:
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
            break
    # OS env tiene prioridad
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if os.getenv(k):
            env[k] = os.environ[k]
    return env


def send(text: str, *, silent: bool = False) -> bool:
    env = _load_env()
    token = env.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("notify: faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID en .env", file=sys.stderr)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text[:4000],
        "parse_mode": "Markdown",
        "disable_notification": "true" if silent else "false",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"notify: error enviando: {e}", file=sys.stderr)
        return False


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("text", help="Mensaje a enviar (Markdown soportado)")
    p.add_argument("--silent", action="store_true", help="No vibrar el celular")
    args = p.parse_args()
    ok = send(args.text, silent=args.silent)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
