"""Tests for src/copybot/crypto_arb.py — gate de LIVE_MODE.

El bot N2 (crypto temporal arb) opera markets crypto-updown-5m con orderbooks
muy thin ($1-5k liquidez). Un BUY de $5-10 mueve el book 0.5-1% → slippage
real del 50-90% → pérdida garantizada. Por eso en LIVE_MODE está OFF por
default y requiere CRYPTO_ARB_ALLOW_LIVE=true explícito.
"""
from __future__ import annotations

import asyncio


def test_crypto_arb_loop_no_arranca_en_live_sin_allow_live(monkeypatch, caplog):
    """LIVE_MODE=true + CRYPTO_ARB_ENABLED=true + sin CRYPTO_ARB_ALLOW_LIVE → return early."""
    import src.copybot.crypto_arb as crypto_arb

    # Forzar el config: enabled=True (CRYPTO_ARB_ENABLED), allow_live=False (default).
    fake_config = crypto_arb.CryptoArbConfig(enabled=True, allow_live=False)
    monkeypatch.setattr(
        crypto_arb.CryptoArbConfig, "from_env",
        classmethod(lambda cls: fake_config),
    )
    # Forzar LIVE_MODE=True visto desde dentro del módulo (la función importa
    # `from src.config import LIVE_MODE` adentro, así que parcheamos en
    # src.config directamente).
    import src.config as cfg
    monkeypatch.setattr(cfg, "LIVE_MODE", True)

    with caplog.at_level("WARNING"):
        asyncio.run(crypto_arb.crypto_arb_loop())

    msgs = " ".join(rec.getMessage() for rec in caplog.records)
    assert "deshabilitado en LIVE_MODE" in msgs


def test_crypto_arb_loop_no_arranca_si_disabled(monkeypatch, caplog):
    """CRYPTO_ARB_ENABLED=false → return early con mensaje 'disabled'."""
    import src.copybot.crypto_arb as crypto_arb

    fake_config = crypto_arb.CryptoArbConfig(enabled=False, allow_live=False)
    monkeypatch.setattr(
        crypto_arb.CryptoArbConfig, "from_env",
        classmethod(lambda cls: fake_config),
    )

    with caplog.at_level("INFO"):
        asyncio.run(crypto_arb.crypto_arb_loop())

    msgs = " ".join(rec.getMessage() for rec in caplog.records)
    assert "disabled" in msgs


def test_config_from_env_lee_allow_live(monkeypatch):
    """CRYPTO_ARB_ALLOW_LIVE=true se lee correctamente del env."""
    import src.copybot.crypto_arb as crypto_arb

    monkeypatch.setenv("CRYPTO_ARB_ENABLED", "true")
    monkeypatch.setenv("CRYPTO_ARB_ALLOW_LIVE", "true")
    cfg = crypto_arb.CryptoArbConfig.from_env()
    assert cfg.enabled is True
    assert cfg.allow_live is True

    monkeypatch.setenv("CRYPTO_ARB_ALLOW_LIVE", "false")
    cfg = crypto_arb.CryptoArbConfig.from_env()
    assert cfg.allow_live is False
