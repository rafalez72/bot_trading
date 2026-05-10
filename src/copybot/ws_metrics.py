"""Métricas in-memory del WS bridge para verificar que está corriendo bien.

Counters + gauges + timestamps que las dos capas (websocket.py y
ws_bridge.py) actualizan en sus eventos clave. El endpoint
``/api/ws-status`` los lee para que podamos chequear desde la PWA o curl
si el WS está vivo, conectado, recibiendo trades, y con cuánta latencia.

Diseño:
- Sin dependencias externas (solo stdlib). Threadsafe vía un único Lock.
- Lectura barata: snapshot() devuelve un dict con todo + derivados (uptime,
  staleness del último mensaje, lag promedio).
- Reset solo en arranque del proceso (no exponer reset por API por ahora).
- El módulo NO loggea — los callers ya loggean con `log.info`. Acá solo
  contamos.

Cross-process snapshot file:
- Las métricas viven en el proceso del **runner** (donde corre el WS
  bridge), pero el endpoint ``/api/ws-status`` corre en el **server**
  (otro contenedor). Para puentearlo, el runner persiste el snapshot a
  ``DATA_DIR/ws_metrics.json`` cada vez que cambia un counter, y el server
  lee ese archivo. Ambos contenedores comparten el mismo volume.
- Atómico vía rename. Si el archivo no existe (server arrancó solo) el
  endpoint devuelve un snapshot vacío.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Lag samples: cuántos guardamos para calcular el promedio
_LAG_WINDOW = 200

# Throttle de la persistencia: como cada frame WS llama a un mutator,
# escribir cada vez sería demasiado IO. Mín. 1s entre escrituras.
_PERSIST_MIN_INTERVAL_S = 1.0

# Path del snapshot. data/ es montada en ambos contenedores.
_SNAPSHOT_PATH = Path(os.getenv("DB_PATH", "data/copybot.db")).parent / "ws_metrics.json"


class _Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._last_persist_at = 0.0
        self._persist_enabled = True
        # Connection lifecycle
        self.connect_attempts = 0
        self.connect_successes = 0
        self.disconnects = 0
        self.reconnects = 0  # disconnects que reentraron al loop
        self.last_connect_at: float | None = None
        self.last_disconnect_at: float | None = None
        self.last_disconnect_reason: str | None = None
        self.connected: bool = False
        # Frame counters
        self.frames_total = 0          # cualquier frame entrante (incluye pongs no-json)
        self.frames_pong = 0           # respuestas a heartbeat
        self.frames_json = 0           # frames JSON parseados
        self.frames_activity = 0       # topic=activity type=trades
        self.frames_matched = 0        # wallet match (post filter)
        self.frames_callback_ok = 0    # _handle_trade completó sin excepción
        self.frames_callback_error = 0
        # Tradebook side effects
        self.opened_buys = 0
        self.closed_sells = 0
        self.skipped_duplicate = 0     # open_position devolvió reason='duplicate'
        self.skipped_other = 0         # open_position devolvió otro reason
        self.bad_payload = 0           # payload sin txh/cid/side válido
        # Diag: contador por reason + últimos N rejects para debugging.
        # In-memory only — al restart se pierde. Persistencia fuera de scope.
        self.reject_reasons: dict[str, int] = {}
        self._recent_rejects: deque = deque(maxlen=50)
        # Gauges
        self.watched_wallets = 0
        # Timestamps de la última actividad (epoch s)
        self.last_msg_at: float | None = None
        self.last_match_at: float | None = None
        self.last_buy_at: float | None = None
        self.last_sell_at: float | None = None
        # Lag samples: payload.timestamp es epoch s del trade en chain;
        # delta vs receipt. Útil para detectar el WS "atrás del polling".
        self._lags: deque[float] = deque(maxlen=_LAG_WINDOW)

    # ---- mutators (todos thread-safe vía Lock) ----

    def on_connect_attempt(self) -> None:
        with self._lock:
            self.connect_attempts += 1

    def on_connect_success(self) -> None:
        with self._lock:
            self.connect_successes += 1
            self.connected = True
            self.last_connect_at = time.time()

    def on_disconnect(self, reason: str | None = None) -> None:
        with self._lock:
            self.disconnects += 1
            if self.last_connect_at is not None:
                self.reconnects += 1
            self.connected = False
            self.last_disconnect_at = time.time()
            if reason is not None:
                self.last_disconnect_reason = str(reason)[:200]

    def on_frame(self, *, json_ok: bool = False) -> None:
        with self._lock:
            self.frames_total += 1
            self.last_msg_at = time.time()
            if json_ok:
                self.frames_json += 1

    def on_pong(self) -> None:
        with self._lock:
            self.frames_pong += 1
            self.last_msg_at = time.time()

    def on_activity_frame(self) -> None:
        with self._lock:
            self.frames_activity += 1

    def on_match(self, *, ts_payload: int | None = None) -> None:
        now = time.time()
        with self._lock:
            self.frames_matched += 1
            self.last_match_at = now
            if ts_payload and ts_payload > 0:
                lag = now - float(ts_payload)
                if -10.0 <= lag <= 600.0:  # filtro defensivo de outliers
                    self._lags.append(lag)

    def on_callback_ok(self) -> None:
        with self._lock:
            self.frames_callback_ok += 1

    def on_callback_error(self) -> None:
        with self._lock:
            self.frames_callback_error += 1

    def on_open_buy(self) -> None:
        with self._lock:
            self.opened_buys += 1
            self.last_buy_at = time.time()

    def on_close_sell(self) -> None:
        with self._lock:
            self.closed_sells += 1
            self.last_sell_at = time.time()

    def on_skip(self, reason: str | None, wallet: str | None = None, cid: str | None = None) -> None:
        with self._lock:
            if reason == "duplicate":
                self.skipped_duplicate += 1
            else:
                self.skipped_other += 1
            # Diag: track top reasons + últimos rejects
            key = reason or "unknown"
            self.reject_reasons[key] = self.reject_reasons.get(key, 0) + 1
            self._recent_rejects.append({
                "ts": time.time(),
                "reason": key,
                "wallet": (wallet or "")[:12],
                "cid": (cid or "")[:12],
            })

    def on_bad_payload(self) -> None:
        with self._lock:
            self.bad_payload += 1

    def set_watched(self, n: int) -> None:
        with self._lock:
            self.watched_wallets = n

    # ---- snapshot ----

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            lags = list(self._lags)
            avg_lag = (sum(lags) / len(lags)) if lags else None
            max_lag = max(lags) if lags else None
            return {
                "uptime_s": round(now - self._started_at, 1),
                "connected": self.connected,
                "watched_wallets": self.watched_wallets,
                "connect": {
                    "attempts": self.connect_attempts,
                    "successes": self.connect_successes,
                    "disconnects": self.disconnects,
                    "reconnects": self.reconnects,
                    "last_connect_at": self.last_connect_at,
                    "last_disconnect_at": self.last_disconnect_at,
                    "last_disconnect_reason": self.last_disconnect_reason,
                    "since_last_connect_s": (
                        round(now - self.last_connect_at, 1)
                        if self.last_connect_at else None
                    ),
                },
                "frames": {
                    "total": self.frames_total,
                    "pong": self.frames_pong,
                    "json": self.frames_json,
                    "activity": self.frames_activity,
                    "matched": self.frames_matched,
                    "callback_ok": self.frames_callback_ok,
                    "callback_error": self.frames_callback_error,
                },
                "trades": {
                    "opened_buys": self.opened_buys,
                    "closed_sells": self.closed_sells,
                    "skipped_duplicate": self.skipped_duplicate,
                    "skipped_other": self.skipped_other,
                    "bad_payload": self.bad_payload,
                    "reject_reasons": dict(self.reject_reasons),
                    "recent_rejects": list(self._recent_rejects)[-10:],
                },
                "last_msg": {
                    "msg_at": self.last_msg_at,
                    "match_at": self.last_match_at,
                    "buy_at": self.last_buy_at,
                    "sell_at": self.last_sell_at,
                    "since_msg_s": (
                        round(now - self.last_msg_at, 1)
                        if self.last_msg_at else None
                    ),
                    "since_match_s": (
                        round(now - self.last_match_at, 1)
                        if self.last_match_at else None
                    ),
                },
                "lag": {
                    "avg_s": round(avg_lag, 3) if avg_lag is not None else None,
                    "max_s": round(max_lag, 3) if max_lag is not None else None,
                    "samples": len(lags),
                },
            }


    # ---- persistencia cross-process ----

    def persist_to_file(self) -> None:
        """Escribe el snapshot al archivo compartido (atómico vía rename).

        Llamado tanto por el runner (en cada mutator vía start_persist_thread)
        como manualmente. Si el directorio no existe o el rename falla, el
        error se silencia con un debug log — no queremos que un fallo de
        observabilidad rompa el bot.
        """
        try:
            snap = self.snapshot()
            snap["_persisted_at"] = time.time()
            target = _SNAPSHOT_PATH
            target.parent.mkdir(parents=True, exist_ok=True)
            # Tempfile en el mismo directorio + os.replace = atómico
            fd, tmp = tempfile.mkstemp(
                prefix=".ws_metrics_", suffix=".tmp", dir=str(target.parent)
            )
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(snap, f, default=str)
                os.replace(tmp, target)
            except Exception:
                # Si el escrito falló pero el tmp quedó, lo borramos.
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as e:
            log.debug("ws_metrics.persist_to_file failed: %s", e)

    def start_persist_thread(self, interval_s: float = 5.0) -> None:
        """Arranca un daemon thread que persiste el snapshot cada interval_s.

        Idempotente: si ya hay uno corriendo (detectado vía un flag),
        no arranca otro. Llamado por ``ws_run_loop`` en el runner.
        """
        with self._lock:
            if getattr(self, "_persist_thread_started", False):
                return
            self._persist_thread_started = True

        def _loop() -> None:
            while True:
                try:
                    time.sleep(interval_s)
                    self.persist_to_file()
                except Exception:
                    pass  # nunca matar el thread

        t = threading.Thread(target=_loop, name="ws_metrics-persist", daemon=True)
        t.start()


def read_snapshot_from_file() -> dict[str, Any] | None:
    """Lee el snapshot persistido por el proceso runner.

    Usado por el server (otro container) que no tiene acceso directo a la
    instancia in-memory del runner. Devuelve ``None`` si el archivo no
    existe o está corrupto (interpretado como "WS off / nunca arrancó").
    """
    try:
        if not _SNAPSHOT_PATH.exists():
            return None
        with _SNAPSHOT_PATH.open("r") as f:
            return json.load(f)
    except Exception as e:
        log.debug("ws_metrics.read_snapshot_from_file failed: %s", e)
        return None


# Singleton (módulo-level).
metrics = _Metrics()
