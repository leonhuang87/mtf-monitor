# -*- coding: utf-8 -*-
"""MTF 策略云端监控面板 — Streamlit Cloud 版。

连 Turso 数据库读取策略状态/交易历史，展示：
  - 当前持仓 + 累计收益
  - 最近信号 + 操作记录
  - 权益曲线
  - K线缓存

每 30 秒自动刷新。

部署到 Streamlit Cloud：
  1. 仓库根目录放本文件 + requirements.txt
  2. Streamlit Cloud Settings 配置 Secrets:
     TURSO_URL = "libsql://xxx.turso.io"
     TURSO_TOKEN = "xxx"
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta

import pandas as pd
import streamlit as st

# 同目录放 db.py 副本（或从 strategy/ 目录复制）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from db import get_conn, init_schema, fetch_all_state, fetch_trades, fetch_equity_curve
except ImportError:
    st.error("无法导入 db.py，请确保 db.py 与 streamlit_app.py 同目录")
    st.stop()


# 东八区
CN_TZ = timezone(timedelta(hours=8))

st.set_page_config(
    page_title="MTF 策略云端监控",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# 紧凑样式
st.markdown("""
<style>
.block-container { padding-top: 1rem !important; padding-bottom: 0 !important; max-width: 100% !important; }
[data-testid="stMetric"] { background: rgba(255,255,255,0.03); border-radius: 6px; padding: 6px 10px !important; }
[data-testid="stMetricValue"] { font-size: 1.15rem !important; }
[data-testid="stMetricLabel"] { font-size: 0.78rem !important; }
div[data-testid="stVerticalBlockBorderWrapper"] { gap: 0.4rem !important; }
.stDataFrame { min-height: 0 !important; }
div[data-testid="stDataFrame"] { max-height: 280px; overflow-y: auto; }
</style>
""", unsafe_allow_html=True)


@st.cache_resource(ttl=300)
def get_db_conn():
    """获取 Turso 连接（缓存 5 分钟避免频繁建连）。"""
    url = st.secrets.get("TURSO_URL", os.environ.get("TURSO_URL", "")).strip()
    token = st.secrets.get("TURSO_TOKEN", os.environ.get("TURSO_TOKEN", "")).strip()
    local = os.environ.get("TURSO_LOCAL_FALLBACK", "data/klines.db")
    if not url or not token:
        st.error("Turso 未配置：请在 Streamlit Cloud Secrets 设置 TURSO_URL 和 TURSO_TOKEN")
        st.stop()
    try:
        conn = get_conn(url, token, local)
        init_schema(conn)
        return conn
    except Exception as e:
        import traceback
        st.error(f"数据库连接失败:\n```\n{traceback.format_exc()}\n```")
        st.stop()


def fmt_ts(ms: int) -> str:
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=CN_TZ).strftime("%m-%d %H:%M")


def fmt_side(pos: int) -> str:
    if pos > 0:
        return "多头 🟢"
    if pos < 0:
        return "空头 🔴"
    return "空仓 ⚪"


def main():
    st.title("📈 MTF 策略云端监控")

    # 60秒自动刷新
    import streamlit.components.v1 as components
    components.html(
        '<script>setTimeout(function(){window.location.reload();}, 60000);</script>',
        height=0,
    )

    # 手动刷新按钮
    col_r1, col_r2 = st.columns([3, 1])
    with col_r2:
        if st.button("🔄 刷新", use_container_width=True):
            st.rerun()

    conn = get_db_conn()

    # 取所有策略状态
    try:
        states = fetch_all_state(conn)
    except Exception as e:
        st.error(f"读取状态失败: {e}")
        states = []

    if not states:
        st.info("暂无策略运行数据，等待下一次策略执行写入。")
        return

    # ---- 策略卡片 ----
    st.subheader("策略概览")
    cols = st.columns(len(states))
    for i, s in enumerate(states):
        with cols[i]:
            sid = s["strategy_id"]
            pos = s["position"]
            eq = s["equity"]
            eq0 = s["equity0"] or 10000.0
            pnl = eq - eq0
            ret = pnl / eq0 * 100 if eq0 > 0 else 0
            delta_color = "normal" if pnl >= 0 else "inverse"

            st.metric(label=f"{sid} 净值", value=f"{eq:.2f}",
                      delta=f"{pnl:+.2f} ({ret:+.2f}%)", delta_color=delta_color)
            st.write(f"持仓: {fmt_side(pos)}")
            if pos != 0:
                st.write(f"开仓价: {s['entry_price']:.2f} | 张数: {s['qty']:.0f}")
            st.write(f"最新信号: {s['last_signal']:+d} | 更新: {fmt_ts(s['updated_at'])}")

            # 冷却状态
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            if s["cooldown_until"] > now_ms:
                remain = (s["cooldown_until"] - now_ms) // 60000
                st.warning(f"冷却中 · 剩余 {remain} 分钟")

    st.divider()

    # ---- 详细数据 ----
    detail_col1, detail_col2 = st.columns(2)

    with detail_col1:
        st.subheader("最近交易")
        all_trades = []
        for s in states:
            try:
                trades = fetch_trades(conn, s["strategy_id"], limit=20)
                for t in trades:
                    t["strategy_id"] = s["strategy_id"]
                    all_trades.append(t)
            except Exception:
                pass
        if all_trades:
            df_t = pd.DataFrame(all_trades)
            df_t["时间"] = df_t["ts"].apply(fmt_ts)
            df_t = df_t.rename(columns={
                "strategy_id": "策略", "action": "动作", "side": "方向",
                "price": "价格", "qty": "张数", "pnl": "盈亏", "reason": "原因",
            })
            df_t["方向"] = df_t["方向"].map({1: "多", -1: "空", 0: "-"})
            display_cols = ["时间", "策略", "动作", "方向", "价格", "张数", "盈亏", "原因"]
            st.dataframe(df_t[display_cols], use_container_width=True, hide_index=True)
        else:
            st.info("暂无交易记录")

    with detail_col2:
        st.subheader("权益曲线（收益率 %）")
        all_curves = []
        for s in states:
            try:
                curve = fetch_equity_curve(conn, s["strategy_id"], limit=200)
                if curve and len(curve) > 1:
                    eq0 = curve[0][1]  # 初始权益
                    for ts, eq in curve:
                        ret_pct = (eq - eq0) / eq0 * 100 if eq0 > 0 else 0
                        all_curves.append({
                            "ts": ts,
                            "时间": datetime.fromtimestamp(ts / 1000, tz=CN_TZ).strftime("%m-%d %H:%M"),
                            "收益率%": ret_pct,
                            "策略": s["strategy_id"],
                        })
            except Exception:
                pass
        if all_curves:
            df_c = pd.DataFrame(all_curves)
            df_pivot = df_c.pivot_table(index="时间", columns="策略", values="收益率%")
            st.line_chart(df_pivot, use_container_width=True)
        else:
            st.info("暂无权益曲线数据（需有平仓记录）")

    # 页脚
    st.caption(f"最后更新: {datetime.now(CN_TZ).strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
