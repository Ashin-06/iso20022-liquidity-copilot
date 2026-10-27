"""Settings, auth, limits, headers, metrics, health, the durable RTGS queue and migrations."""
import hashlib
import importlib
import json
import os
import subprocess
import sys
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from liqcopilot import db, ops
from liqcopilot.iso20022 import ISO20022Payment, generate_iso20022_xml
from liqcopilot.store import to_paise

KEY = "liqcopilot_" + "x" * 40
HASHED = {f"sha256:{hashlib.sha256(KEY.encode()).hexdigest()}": {"user": "rtgs-ops", "role": "operator"}}
BANKS = ["SBININBBXXX", "HDFCINBBXXX", "ICICINBBXXX", "AXISINBBXXX"]
# A four-bank ring: each bank owes the next 100 crore but holds only 10 crore above its floor.
POSITIONS = {"participants": [{"bic": b, "name": b[:4], "opening_balance": "110.000000001", "reserve_floor": "100"}
                              for b in BANKS]}
RING = {"payments": [{"payment_id": f"RING-{i}", "sender": BANKS[i], "receiver": BANKS[(i + 1) % 4],
                      "amount": "100.25", "priority": "HIGH"} for i in range(4)]}


def boot(monkeypatch, **env):
    for k in [k for k in os.environ if k.startswith("LIQCOPILOT_")]:
        monkeypatch.delenv(k)
    for k, v in env.items():
        monkeypatch.setenv(f"LIQCOPILOT_{k}", v)
    import liqcopilot.api as api

    return importlib.reload(api)


@pytest.mark.parametrize("change, message", [
    ({"LIQCOPILOT_API_KEYS": None}, "API keys"),
    ({"LIQCOPILOT_DATABASE_URL": None}, "DATABASE_URL"),
    ({"LIQCOPILOT_API_KEYS": json.dumps({KEY: {"user": "p", "role": "operator"}})}, "sha256"),
    ({"LIQCOPILOT_API_KEYS": json.dumps({"sha256:" + "0" * 64: {"user": "x", "role": "root"}})}, "roles"),
    ({"LIQCOPILOT_CORS_ORIGINS": "*"}, "CORS"),
])
def test_production_settings_refused(change, message):
    env = {"LIQCOPILOT_ENV": "production", "LIQCOPILOT_API_KEYS": json.dumps(HASHED), "LIQCOPILOT_DATABASE_URL": "sqlite://"}
    env.update(change)
    with pytest.raises(ops.ConfigError, match=message):
        ops.load_settings({k: v for k, v in env.items() if v is not None})
    ok = {k: v for k, v in env.items() if k not in change}
    if len(ok) == 3:
        assert ops.load_settings(ok).production


def test_no_public_bind_and_readable_exit():
    with pytest.raises(ops.ConfigError, match="without API keys"):
        ops.load_settings({"LIQCOPILOT_HOST": "0.0.0.0"})
    env = {k: v for k, v in os.environ.items() if not k.startswith("LIQCOPILOT_")}
    r = subprocess.run([sys.executable, "-m", "liqcopilot", "serve", "--host", "0.0.0.0"], env=env,
                       capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode != 0 and "without API keys" in r.stderr


def test_roles_decide_what_a_key_may_do(monkeypatch):
    keys = {"k-bank": {"user": "sbi-gw", "role": "participant"}, "k-desk": {"user": "treasury", "role": "analyst"},
            "k-ops": {"user": "rtgs-ops", "role": "operator"}, "k-mon": {"user": "prom", "role": "monitor"}}
    c = TestClient(boot(monkeypatch, API_KEYS=json.dumps(keys)).app)
    h = {k: {"x-api-key": k} for k in keys}
    assert c.put("/rtgs/participants", json=POSITIONS).status_code == 401
    assert c.put("/rtgs/participants", json=POSITIONS, headers=h["k-bank"]).status_code == 403
    assert c.put("/rtgs/participants", json=POSITIONS, headers=h["k-ops"]).status_code == 200
    assert c.post("/rtgs/payments", json=RING, headers=h["k-bank"]).status_code == 200
    assert c.post("/rtgs/payments", json=RING, headers=h["k-desk"]).status_code == 403
    assert c.post("/rtgs/settle", json={}, headers=h["k-bank"]).status_code == 403
    assert c.post("/settle", json={"scenario_id": "ring_4bank_classic"}, headers=h["k-desk"]).status_code == 200
    assert c.post("/settle", json={"scenario_id": "ring_4bank_classic"}, headers=h["k-bank"]).status_code == 403
    assert c.get("/metrics", headers=h["k-ops"]).status_code == 403
    m = c.get("/metrics", headers=h["k-mon"])
    assert m.status_code == 200 and 'liqcopilot_decisions_total{decision="queued"} 4.0' in m.text


def test_queue_settles_books_balances_and_survives_a_restart(monkeypatch, tmp_path):
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'rtgs.db'}"
    api = boot(monkeypatch, DATABASE_URL=url)
    with api.store.db.begin() as cx:  # a clean slate when TEST_DATABASE_URL is shared
        for t in ("queued_payments", "participants", "settlement_runs"):
            cx.execute(text(f"DELETE FROM {t}"))  # nosec B608: fixed table names
    c = TestClient(api.app)
    assert c.put("/rtgs/participants", json=POSITIONS).status_code == 200
    assert c.post("/rtgs/payments", json=RING).json()["queued"] == [f"RING-{i}" for i in range(4)]
    assert c.post("/rtgs/payments", json=RING).status_code == 409  # duplicate IDs
    stranger = {"payments": [dict(RING["payments"][0], payment_id="X1", receiver="KKBKINBBXXX")]}
    assert c.post("/rtgs/payments", json=stranger).status_code == 400  # not a participant

    fifo = c.post("/rtgs/settle", json={"engine": "fifo"}).json()  # gross RTGS: gridlocked
    assert fifo["settled_count"] == 0 and fifo["queued_count"] == 4

    api = boot(monkeypatch, DATABASE_URL=url)  # restart: the queue is still there
    c = TestClient(api.app)
    assert c.get("/rtgs/payments", params={"status": "queued"}).json()["count"] == 4
    lsm = c.post("/rtgs/settle", json={"engine": "multilateral_lsm"}).json()
    assert lsm["settled_count"] == 4 and lsm["run_id"]
    assert c.get("/rtgs/payments", params={"status": "queued"}).json()["count"] == 0
    parts = {p["bic"]: p for p in c.get("/rtgs/participants").json()["participants"]}
    # the ring nets to zero: every balance is back where it started, to the paisa
    assert all(Decimal(str(p["opening_balance"])) == Decimal("110.000000001") for p in parts.values())
    runs = c.get("/rtgs/runs").json()["runs"]
    assert runs[0]["engine"] == "multilateral_lsm" and Decimal(runs[0]["settled_volume"]) == Decimal("401.00")
    with api.store.db.connect() as cx:
        assert cx.execute(text("SELECT amount_paise FROM queued_payments WHERE payment_id = 'RING-0'")).scalar() == 100_250_000_000
    assert c.post("/rtgs/settle", json={}).json()["settled_count"] == 0  # nothing left


def test_money_is_exact(monkeypatch):
    c = TestClient(boot(monkeypatch).app)
    c.put("/rtgs/participants", json=POSITIONS)
    sub_paisa = {"payments": [dict(RING["payments"][0], payment_id="P-SUB", amount="1.0000000001")]}
    assert c.post("/rtgs/payments", json=sub_paisa).status_code == 422
    for bad in ("NaN", "Infinity", "-5"):
        assert c.post("/rtgs/payments", json={"payments": [dict(RING["payments"][0], amount=bad)]}).status_code == 422
    assert to_paise(Decimal("0.1") + Decimal("0.2")) == 300_000_000  # 0.30 crore, exactly
    with pytest.raises(ValueError):
        to_paise(Decimal("1.0000000005"))


def test_iso20022_message_can_be_queued(monkeypatch):
    c = TestClient(boot(monkeypatch).app)
    c.put("/rtgs/participants", json=POSITIONS)
    xml = generate_iso20022_xml(ISO20022Payment(msg_id="M1", end_to_end_id="E2E-1", debtor_agent=BANKS[0],
                                                creditor_agent=BANKS[1], amount=Decimal("12.50"), priority="URGT"))
    assert c.post("/rtgs/payments", json={"xml": xml}).json()["queued"] == ["E2E-1"]
    row = c.get("/rtgs/payments").json()["payments"][0]
    assert row["payment_id"] == "E2E-1" and Decimal(row["amount"]) == Decimal("12.50")
    assert c.post("/rtgs/payments", json={"xml": "<not-xml"}).status_code == 400


def test_limits_headers_and_errors(monkeypatch):
    c = TestClient(boot(monkeypatch, RATE_LIMIT="3").app)
    codes = [c.get("/rtgs/participants").status_code for _ in range(5)]
    assert codes[:3] == [200] * 3 and codes[3:] == [429, 429]
    api = boot(monkeypatch, MAX_BODY_BYTES="500")

    @api.app.get("/boom")
    def boom():
        raise RuntimeError("secret detail: SBIN balance 4500")

    c = TestClient(api.app)
    assert c.post("/iso20022/parse", json={"xml": "x" * 600}).status_code == 413

    def chunks():
        for _ in range(10):
            yield b"a" * 100

    assert c.post("/settle", content=chunks(), headers={"content-type": "application/json"}).status_code == 413
    crash = c.get("/boom")
    assert crash.status_code == 500 and "4500" not in crash.text and crash.json()["request_id"]
    bad = c.post("/settle", json={"engine": "fifo", "scenario_id": "SECRET-SCENARIO"})
    assert bad.status_code == 400 and "SECRET" not in bad.text
    r = c.get("/rtgs/participants", headers={"x-request-id": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123" and r.headers["content-security-policy"] == ops.API_CSP
    assert "unsafe-inline" in c.get("/").headers["content-security-policy"]


def test_health_and_readiness(monkeypatch):
    api = boot(monkeypatch)
    c = TestClient(api.app)
    assert c.get("/healthz").json() == {"status": "ok"}
    assert c.get("/readyz").json()["checks"] == {"database": True}
    monkeypatch.setattr(api.store.db, "connect", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert c.get("/readyz").status_code == 503


def test_migrations_apply_once(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    eng = db.engine(url)
    assert db.migrate(eng) == ["001_init.sql"] and db.migrate(eng) == []
    with eng.connect() as c:
        tables = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))}
    assert {"participants", "queued_payments", "settlement_runs", "schema_migrations"} <= tables
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("LIQCOPILOT_")}, "LIQCOPILOT_DATABASE_URL": url}
    out = subprocess.run([sys.executable, "-m", "liqcopilot", "migrate"], env=env, capture_output=True, text=True,
                         timeout=120, check=False)
    assert out.returncode == 0 and "up to date" in out.stdout
