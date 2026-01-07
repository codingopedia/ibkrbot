from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Tuple


def load_pnl_series(sqlite_path: str, symbol: str) -> List[Tuple[str, float]]:
    conn = sqlite3.connect(sqlite_path)
    rows = conn.execute(
        """
        SELECT ts, (realized_usd + unrealized_usd - commissions_usd) AS pnl
        FROM pnl_snapshots
        WHERE symbol=?
        ORDER BY ts ASC
        """,
        (symbol,),
    ).fetchall()
    conn.close()
    return [(r[0], float(r[1])) for r in rows]
