"""Отчёт по поинтам: каждый аккаунт × каждый проект с поинтами, в ячейке «мои / 10-е место лидерборда».

Источники (те же, что у сайтов проектов):
  Kalos    — api.kalos.fun: свои поинты сезона после входа по подписи, лидерборд /v2/points?period=season.
  BunnyPad — поинты считаются из событий лаунчпада в сети ровно по коду bunnypad.fun/points (v1).
  Elyra    — useelyra.xyz/api/points/<адрес> и /api/leaderboard.
  ELYSA    — их база Supabase (таблица players) публичным ключом с elysa.fun/config.js.
  Hunt     — EXP из контракта игры (userExp), 10-е место — /api/leaderboard.

    python points.py              # таблица по всем аккаунтам
    python points.py --limit 5    # первые N
Из main.py таблица печатается раз в points_every_hours часов ([points] в config.ini).
"""
from __future__ import annotations

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from curl_cffi import requests as http
from web3 import Web3

from common import load_accounts, load_config, log, short

ELYSIUM_RPC = "https://testnet-rpc.elysium.kinetiq.xyz"
PROJECTS = ["Kalos", "BunnyPad", "Elyra", "ELYSA", "Hunt EXP"]


def _get(url: str, **kw):
    return http.get(url, impersonate="chrome136", timeout=40, **kw).json()


def _w3() -> Web3:
    return Web3(Web3.HTTPProvider(ELYSIUM_RPC, request_kwargs={"timeout": 60}))


# ── Kalos ────────────────────────────────────────────────────────────────────
def kalos_top10() -> float | None:
    rows = _get("https://api.kalos.fun/v2/points?period=season&limit=10&offset=0")["rows"]
    return float(rows[9]["points"]) if len(rows) >= 10 else None


def kalos_mine(acc: dict) -> float:
    import kalos
    d = kalos._session(acc).get(kalos.API + "/v2/points/me/breakdown?period=season").json()
    return float(d["season"]["points"])


# ── BunnyPad: пересчёт из событий, как на bunnypad.fun/points ────────────────
BUNNY_LP = "0x3c4F47959101557Eed9Bc4ae8D8D986aE8d3E5b1"
BUNNY_FROM = 11518824
_BUNNY_EVENTS = [
    {"anonymous": False, "name": "TokenLaunched", "type": "event", "inputs": [
        {"indexed": True, "name": "token", "type": "address"}, {"indexed": True, "name": "creator", "type": "address"},
        {"indexed": True, "name": "quote", "type": "address"}, {"indexed": False, "name": "name", "type": "string"},
        {"indexed": False, "name": "symbol", "type": "string"},
        {"indexed": False, "name": "feeRecipient", "type": "address"},
        {"indexed": False, "name": "holderMode", "type": "bool"}, {"indexed": False, "name": "buyTaxBps", "type": "uint16"},
        {"indexed": False, "name": "sellTaxBps", "type": "uint16"},
        {"indexed": False, "name": "virtualQuote", "type": "uint256"},
        {"indexed": False, "name": "antiSnipeUntil", "type": "uint256"}]},
    {"anonymous": False, "name": "Trade", "type": "event", "inputs": [
        {"indexed": True, "name": "token", "type": "address"}, {"indexed": True, "name": "trader", "type": "address"},
        {"indexed": False, "name": "isBuy", "type": "bool"}, {"indexed": False, "name": "quoteAmount", "type": "uint256"},
        {"indexed": False, "name": "tokenAmount", "type": "uint256"}, {"indexed": False, "name": "fee", "type": "uint256"},
        {"indexed": False, "name": "realQuote", "type": "uint256"},
        {"indexed": False, "name": "tokenReserve", "type": "uint256"}]},
]
# правила v1 с их страницы: запуск +100 (первые 10), первый запуск +50, сделка от $1 +10 (до 30 на токен),
# первая покупка +25, первая продажа +25, объём +0.1 за $1 (до $1000 за сделку), HYPE = $40, USDC = $1
_PRICE = {"0x0000000000000000000000000000000000000000": (40, 18), "0x7d29d8047b905000459c0e80c34a26ceedcb47b2": (1, 6)}


def bunny_board() -> dict[str, int]:
    """{адрес: поинты} всех участников BunnyPad."""
    w3 = _w3()
    c = w3.eth.contract(address=BUNNY_LP, abi=_BUNNY_EVENTS)
    head, events = w3.eth.block_number, []
    for a in range(BUNNY_FROM, head + 1, 2_000_000):
        b = min(a + 2_000_000 - 1, head)
        events += [("launch", e) for e in c.events.TokenLaunched.get_logs(from_block=a, to_block=b)]
        events += [("trade", e) for e in c.events.Trade.get_logs(from_block=a, to_block=b)]
    events.sort(key=lambda x: (x[1]["blockNumber"], x[1]["logIndex"]))
    quote_of, per_token, st = {}, {}, {}
    me_lp = BUNNY_LP.lower()

    def s(addr):
        return st.setdefault(addr, {"launch": 0, "launches": 0, "trade": 0, "vol": 0.0, "bonus": 0, "buys": 0, "sells": 0})

    for kind, e in events:
        a = e["args"]
        if kind == "launch":
            quote_of[a["token"].lower()] = a["quote"].lower()
            w = s(a["creator"].lower())
            if w["launches"] < 10:
                w["launch"] += 100
                if w["launches"] == 0:
                    w["bonus"] += 50
            w["launches"] += 1
            continue
        trader = a["trader"].lower()
        if trader == me_lp:
            continue
        q = quote_of.get(a["token"].lower())
        if q not in _PRICE:
            continue
        price, dec = _PRICE[q]
        usd = a["quoteAmount"] / 10**dec * price
        if usd < 1:
            continue
        k = trader + a["token"].lower()
        if per_token.get(k, 0) >= 30:
            continue
        per_token[k] = per_token.get(k, 0) + 1
        w = s(trader)
        if a["isBuy"] and w["buys"] == 0:
            w["bonus"] += 25
        if not a["isBuy"] and w["sells"] == 0:
            w["bonus"] += 25
        w["buys" if a["isBuy"] else "sells"] += 1
        w["trade"] += 10
        w["vol"] += min(usd, 1000) * 0.1
    return {k: v["launch"] + v["trade"] + int(v["vol"]) + v["bonus"] for k, v in st.items()}


# ── Elyra ────────────────────────────────────────────────────────────────────
def elyra_top10() -> float | None:
    rows = sorted((r["total"] for r in _get("https://useelyra.xyz/api/leaderboard")["rows"]), reverse=True)
    return rows[9] if len(rows) >= 10 else None


def elyra_mine(addr: str) -> float:
    return float(_get(f"https://useelyra.xyz/api/points/{addr}")["points"]["total"])


# ── ELYSA ────────────────────────────────────────────────────────────────────
def _elysa_db():
    js = http.get("https://elysa.fun/config.js", impersonate="chrome136", timeout=30).text
    m = re.search(r"POINTS\s*=\s*\{\s*url:\s*'([^']+)',\s*key:\s*'([^']+)'", js)
    if not m:
        raise RuntimeError("elysa: не нашёл адрес базы поинтов")
    return m.group(1), {"apikey": m.group(2), "Authorization": "Bearer " + m.group(2)}


def elysa_board(addrs: list[str]) -> tuple[float | None, dict[str, float]]:
    url, h = _elysa_db()
    top = _get(url + "/rest/v1/players?select=points&order=points.desc&limit=10", headers=h)
    lst = ",".join(a.lower() for a in addrs)
    mine = _get(url + f"/rest/v1/players?select=address,points&address=in.({lst})", headers=h)
    return (float(top[9]["points"]) if len(top) >= 10 else None), {r["address"]: float(r["points"]) for r in mine}


# ── Elysium Hunt ─────────────────────────────────────────────────────────────
HUNT_CORE = "0x896b17b15C7acB6Dcde257d07f4Ab3628Bbc5b52"


def hunt_top10() -> float | None:
    ops = _get("https://elysium.archuntx.site/api/leaderboard")["operatives"]
    ten = [o for o in ops if o.get("rank") == 10]
    return float(ten[0]["exp"]) if ten else None


def hunt_mine(addr: str) -> float:
    c = _w3().eth.contract(address=HUNT_CORE, abi=[{"type": "function", "name": "userExp", "stateMutability": "view",
                                                    "inputs": [{"name": "a", "type": "address"}],
                                                    "outputs": [{"type": "uint256"}]}])
    return float(c.functions.userExp(addr).call())


# ── сборка и печать ──────────────────────────────────────────────────────────
def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:
        log.debug("points: %s: %s", getattr(fn, "__name__", fn), e)
        return None


def collect(accounts: list[dict]) -> tuple[dict, dict]:
    """→ (top10 {проект: число}, mine {номер аккаунта: {проект: число}})"""
    addrs = [a["address"] for a in accounts]
    top = {"Kalos": _safe(kalos_top10), "Elyra": _safe(elyra_top10), "Hunt EXP": _safe(hunt_top10)}
    bunny = _safe(bunny_board) or {}
    b_sorted = sorted(bunny.values(), reverse=True)
    top["BunnyPad"] = b_sorted[9] if len(b_sorted) >= 10 else None
    el = _safe(elysa_board, addrs)
    top["ELYSA"], elysa_mine = el if el else (None, {})

    def one(acc):
        a = acc["address"]
        return acc["n"], {"Kalos": _safe(kalos_mine, acc), "BunnyPad": bunny.get(a.lower(), 0) if bunny else None,
                          "Elyra": _safe(elyra_mine, a), "ELYSA": elysa_mine.get(a.lower(), 0) if el else None,
                          "Hunt EXP": _safe(hunt_mine, a)}

    with ThreadPoolExecutor(8) as ex:
        mine = dict(ex.map(one, accounts))
    return top, mine


def _num(v) -> str:
    if v is None:
        return "—"
    if v >= 1_000_000:
        return f"{v / 1e6:.1f}M"
    if v >= 10_000:
        return f"{v / 1e3:.1f}k"
    return f"{v:.0f}" if v >= 100 or v == int(v) else f"{v:.1f}"


def table(accounts: list[dict], top: dict, mine: dict) -> str:
    head = ["#", "кошелёк"] + [f"{p} (мои / 10-е)" for p in PROJECTS]
    rows = []
    for acc in accounts:
        m = mine.get(acc["n"], {})
        rows.append([str(acc["n"]), short(acc["address"])] + [f"{_num(m.get(p))} / {_num(top.get(p))}" for p in PROJECTS])
    total = ["Σ", "сумма"] + [_num(sum(mine[n].get(p) or 0 for n in mine)) for p in PROJECTS]
    width = [max(len(r[i]) for r in [head, total] + rows) for i in range(len(head))]
    fmt = lambda r: "  ".join(c.rjust(w) if i != 1 else c.ljust(w) for i, (c, w) in enumerate(zip(r, width)))
    line = "-" * len(fmt(head))
    return "\n".join([line, fmt(head), line] + [fmt(r) for r in rows] + [line, fmt(total), line])


def report(accounts: list[dict]) -> str:
    top, mine = collect(accounts)
    return table(accounts, top, mine)


def main() -> None:
    ap = argparse.ArgumentParser(description="Поинты по всем аккаунтам и проектам")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    load_config()
    print(report(load_accounts()[: args.limit]))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
