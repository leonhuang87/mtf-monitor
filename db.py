# -*- coding: utf-8 -*-
"""数据库适配层 — Turso (libSQL) 优先，本地 SQLite 回退。

表结构：
  klines(inst_id, ts, open, high, low, close, vol, volCcy, volCcyQuote)  -- K线缓存
  state(strategy_id PK, position, entry_price, qty, cooldown_until,
        last_bar_ts, last_signal, equity, equity0, updated_at)           -- 策略状态
  trades(id PK, strategy_id, ts, action, side, price, qty, fee, pnl,
         reason, bar_ts)                                                 -- 交易历史

libSQL 与 SQLite 的 dbapi2 接口一致，仅连接方式不同：
  - libsql_experimental.connect(url, auth_token=...)
  - sqlite3.connect(path)
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# libSQL 可选导入（本地无 turso 时回退到 sqlite3）
try:
    import libsql_experimental as libsql
    _LIBSQL_AVAILABLE = True
except ImportError:
    libsql = None
    _LIBSQL_AVAILABLE = False


def get_conn(turso_url: str | None = None, turso_token: str | None = None,
             local_path: str = "data/klines.db"):
    """获取数据库连接。

    优先 Turso（libSQL），无配置时回退本地 SQLite。
    返回 dbapi2 兼容连接。
    """
    if turso_url and turso_token and _LIBSQL_AVAILABLE:
        try:
            conn = libsql.connect(turso_url, auth_token=turso_token)
            logger.info(f"[db] 已连接 Turso: {turso_url}")
            return conn
        except Exception as e:
            logger.warning(f"[db] Turso 连接失败，回退本地 SQLite: {e}")

    # 本地 SQLite 回退
    path = Path(local_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    logger.info(f"[db] 已连接本地 SQLite: {path}")
    return conn


def init_schema(conn) -> None:
    """建表（幂等）。libSQL 与 SQLite 语法一致。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS klines (
            inst_id TEXT NOT NULL,
            ts      INTEGER NOT NULL,
            open    REAL NOT NULL,
            high    REAL NOT NULL,
            low     REAL NOT NULL,
            close   REAL NOT NULL,
            vol     REAL NOT NULL,
            volCcy  REAL NOT NULL,
            volCcyQuote REAL NOT NULL,
            PRIMARY KEY (inst_id, ts)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_klines_ts ON klines(inst_id, ts);")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS state (
            strategy_id   TEXT PRIMARY KEY,
            position      INTEGER NOT NULL DEFAULT 0,
            entry_price   REAL NOT NULL DEFAULT 0,
            qty           REAL NOT NULL DEFAULT 0,
            cooldown_until INTEGER NOT NULL DEFAULT 0,
            last_bar_ts   INTEGER NOT NULL DEFAULT 0,
            last_signal   INTEGER NOT NULL DEFAULT 0,
            equity        REAL NOT NULL DEFAULT 0,
            equity0       REAL NOT NULL DEFAULT 0,
            updated_at    INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id TEXT NOT NULL,
            ts          INTEGER NOT NULL,
            action      TEXT NOT NULL,
            side        INTEGER NOT NULL,
            price       REAL NOT NULL,
            qty         REAL NOT NULL,
            fee         REAL NOT NULL,
            pnl         REAL NOT NULL,
            reason      TEXT,
            bar_ts      INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_strat_ts ON trades(strategy_id, ts DESC);"
    )
    conn.commit()


def fetch_all_state(conn) -> list[dict]:
    """取所有策略状态（监控页用）。"""
    cur = conn.execute(
        "SELECT strategy_id, position, entry_price, qty, cooldown_until, "
        "last_bar_ts, last_signal, equity, equity0, updated_at FROM state "
        "ORDER BY strategy_id"
    )
    rows = cur.fetchall()
    return [
        {
            "strategy_id": r[0], "position": int(r[1]), "entry_price": float(r[2]),
            "qty": float(r[3]), "cooldown_until": int(r[4]), "last_bar_ts": int(r[5]),
            "last_signal": int(r[6]), "equity": float(r[7]), "equity0": float(r[8]),
            "updated_at": int(r[9]),
        }
        for r in rows
    ]


def fetch_trades(conn, strategy_id: str, limit: int = 50) -> list[dict]:
    """取最近 N 笔交易（按 ts 降序）。"""
    cur = conn.execute(
        "SELECT ts, action, side, price, qty, fee, pnl, reason, bar_ts "
        "FROM trades WHERE strategy_id=? ORDER BY ts DESC LIMIT ?",
        (strategy_id, limit),
    )
    rows = cur.fetchall()
    return [
        {
            "ts": int(r[0]), "action": r[1], "side": int(r[2]), "price": float(r[3]),
            "qty": float(r[4]), "fee": float(r[5]), "pnl": float(r[6]),
            "reason": r[7] or "", "bar_ts": int(r[8]),
        }
        for r in rows
    ]


def load_state(conn, strategy_id: str, equity0: float = 0.0) -> dict[str, Any]:
    """加载策略状态，不存在返回默认。"""
    cur = conn.execute(
        "SELECT position, entry_price, qty, cooldown_until, last_bar_ts, "
        "last_signal, equity, equity0, updated_at "
        "FROM state WHERE strategy_id=?",
        (strategy_id,),
    )
    row = cur.fetchone()
    if row is None:
        return {
            "strategy_id": strategy_id, "position": 0, "entry_price": 0.0,
            "qty": 0.0, "cooldown_until": 0, "last_bar_ts": 0,
            "last_signal": 0, "equity": equity0, "equity0": equity0, "updated_at": 0,
        }
    return {
        "strategy_id": strategy_id, "position": int(row[0]),
        "entry_price": float(row[1]), "qty": float(row[2]),
        "cooldown_until": int(row[3]), "last_bar_ts": int(row[4]),
        "last_signal": int(row[5]), "equity": float(row[6]),
        "equity0": float(row[7]), "updated_at": int(row[8]),
    }


def fetch_equity_curve(conn, strategy_id: str, limit: int = 500) -> list[tuple[int, float]]:
    """从 trades 表构建权益曲线（取平仓动作的累计 pnl）。

    简化版：按时间正序返回 (ts, cumulative_equity)。
    """
    cur = conn.execute(
        "SELECT ts, pnl, action FROM trades WHERE strategy_id=? ORDER BY ts ASC",
        (strategy_id,),
    )
    rows = cur.fetchall()
    if not rows:
        return []
    state = load_state(conn, strategy_id)
    equity = state["equity0"]
    curve = [(int(rows[0][0]), equity)]
    for r in rows:
        ts, pnl, action = int(r[0]), float(r[1]), r[2]
        if action.startswith("close") and pnl != 0:
            equity += pnl
            curve.append((ts, equity))
    return curve[-limit:]
