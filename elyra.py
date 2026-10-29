"""Elyra (useelyra.xyz) — лендинг на Elysium в стиле Morpho. Поинты начисляются каждый час за открытые позиции:
HYPE даёт больше, займ считается вдвое.

faucet(): клейм фосета Elyra (100 USDC + 100 ELYRA раз в 24 ч, только газ).
run():
  1. разрешение hypeGateway в пуле (один раз; без него депозиты HYPE отклоняются: Unauthorized);
  2. депозит HYPE в рынок, где занимают WHYPE (supplyHYPE);
  3. депозит части USDC в рынок, где занимают USDC (supply);
  4. весь ELYRA с фосета — залогом в рынок ELYRA → USDC без займа (elyra_collateral = 1);
  5. по желанию: залог HYPE + небольшой займ USDC (elyra_borrow = 1).
Позиции не закрываются — поинты идут, пока они открыты.
"""
from __future__ import annotations

import time

from common import Config, log
from onchain import (E, call, contract, cs, ensure_allowance, erc20_balance, fmt, fmt_units, pause, rand_amount,
                     send_tx, w3, with_retry)

POOL = "0x9e4a4632985CBFC13c7143F121Ff0F99a0445bd1"
GATEWAY = "0x44A8cF3dDc3a48CF2A0dcF59AAe830171f05Ea5e"
LENS = "0xAc660884b4cF867593564C70b1370700781bE8f9"
FAUCET = "0xC2E788b2Ba82e32bd8B103D15F15AF7176b44E4E"
WHYPE = "0xCD57F65c2B0e5881CFC2E609F7CD53b746E1F234"
USDC = "0x7D29d8047B905000459c0E80c34a26CeedcB47b2"
ELYRA = "0xdFa4D8fE7EC1B2982790C485Eb6B277E2df010f8"
ADAPTIVE_IRM = "0xD29B7f39d2A4816BbE3e1ae203aF1C96B9d5bf61"
LLTV_CORE = 0xaaf96eb9d0d0000  # 77%

# MarketParams: (loanToken, collateralToken, oracle, irm, lltv)
MARKET_WHYPE_LOAN = (cs(WHYPE), cs(USDC), cs("0x84c682039310e88913ea37232cf55b186a2808e1"), cs(ADAPTIVE_IRM), LLTV_CORE)
MARKET_USDC_LOAN = (cs(USDC), cs(WHYPE), cs("0xc7927d427089c7f3c3db9a4464aa159cbc96525f"), cs(ADAPTIVE_IRM), LLTV_CORE)
# рынок «залог ELYRA → займ USDC» (frontier): kink-ставка, ликвидация при 38.5%; без займа ликвидации нет
KINK_IRM = "0x03c02eb7029e2064a10d82234d97027f121fb3dd"
LLTV_FRONTIER = 0x557cb75ce868000  # 38.5%
MARKET_ELYRA_COLLATERAL = (cs(USDC), cs(ELYRA), cs("0x9f23d0c9d802c05baa3deb3e912280972bb46de4"), cs(KINK_IRM),
                           LLTV_FRONTIER)

_MP = {"name": "params", "type": "tuple", "components": [
    {"name": "loanToken", "type": "address"}, {"name": "collateralToken", "type": "address"},
    {"name": "oracle", "type": "address"}, {"name": "irm", "type": "address"}, {"name": "lltv", "type": "uint256"}]}
_A = lambda n: {"name": n, "type": "address"}
_U = lambda n: {"name": n, "type": "uint256"}

POOL_ABI = [
    {"type": "function", "name": "isAuthorized", "stateMutability": "view", "inputs": [_A("a"), _A("b")],
     "outputs": [{"type": "bool"}]},
    {"type": "function", "name": "setAuthorization", "stateMutability": "nonpayable",
     "inputs": [_A("authorized"), {"name": "allowed", "type": "bool"}], "outputs": []},
    {"type": "function", "name": "supplyCollateral", "stateMutability": "nonpayable",
     "inputs": [_MP, _U("assets"), _A("onBehalf"), {"name": "data", "type": "bytes"}], "outputs": []},
    {"type": "function", "name": "supply", "stateMutability": "nonpayable",
     "inputs": [_MP, _U("assets"), _U("shares"), _A("onBehalf"), {"name": "data", "type": "bytes"}],
     "outputs": [{"type": "uint256"}, {"type": "uint256"}]},
    {"type": "function", "name": "borrow", "stateMutability": "nonpayable",
     "inputs": [_MP, _U("assets"), _U("shares"), _A("onBehalf"), _A("receiver")],
     "outputs": [{"type": "uint256"}, {"type": "uint256"}]},
]
GATEWAY_ABI = [
    {"type": "function", "name": n, "stateMutability": "payable", "inputs": [_MP, _A("onBehalf")], "outputs": []}
    for n in ("supplyHYPE", "supplyCollateralHYPE")
]
LENS_ABI = [
    {"type": "function", "name": "positionView", "stateMutability": "view", "inputs": [_MP, _A("user")],
     "outputs": [{"type": "tuple", "components": [
         _U("supplyAssets"), _U("supplyShares"), _U("borrowAssets"), _U("borrowShares"), _U("collateral"),
         _U("healthFactor"), _U("maxBorrow"), _U("liquidationPrice")]}]},
]
FAUCET_ABI = [
    {"type": "function", "name": "claim", "stateMutability": "nonpayable", "inputs": [], "outputs": []},
    {"type": "function", "name": "nextClaimAt", "stateMutability": "view", "inputs": [_A("a")],
     "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "paused", "stateMutability": "view", "inputs": [], "outputs": [{"type": "bool"}]},
    {"type": "function", "name": "elyraAmount", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "usdcAmount", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
]


def faucet(acc: dict, cfg: Config, tag: str) -> None:
    if not (cfg.opt("elyra", "elyra", 0) and cfg.opt("elyra", "elyra_faucet", 1)):
        return
    f = contract(E, FAUCET, FAUCET_ABI, acc)
    if call(f.functions.paused()):
        log.info("%s: elyra: фосет на паузе", tag)
        return
    nxt = call(f.functions.nextClaimAt(acc["address"]))
    if nxt > time.time():
        log.info("%s: elyra: фосет уже клеймили, следующий через %.1f ч", tag, (nxt - time.time()) / 3600)
        return
    me = acc["address"]
    before = erc20_balance(E, USDC, me, acc), erc20_balance(E, ELYRA, me, acc)
    link = send_tx(E, acc, FAUCET, f.encode_abi("claim"))
    # считаем по балансам: когда USDC в фосете кончается, он выдаёт только ELYRA
    got_usdc = erc20_balance(E, USDC, me, acc) - before[0]
    got_elyra = erc20_balance(E, ELYRA, me, acc) - before[1]
    log.info("%s: elyra: фосет — %s USDC + %s ELYRA, %s", tag, fmt_units(got_usdc, 6), fmt(got_elyra), link)


def borrow_against_elyra(acc: dict, cfg: Config, tag: str, pool) -> None:
    """Займ USDC под залог ELYRA (ELYRA бесплатный, с фосета) и депозит занятого USDC в рынок USDC.
    Поинты: займ ×2 + депозит ×1 на ту же сумму; HYPE не тратится. Берётся доля лимита elyra_borrow_elyra_share:
    при 40% ликвидация наступит, только если ELYRA подешевеет больше чем на 60%."""
    me = acc["address"]
    pos = call(contract(E, LENS, LENS_ABI, acc).functions.positionView(MARKET_ELYRA_COLLATERAL, me))
    borrowed, max_borrow = pos[2], pos[6]
    extra = int(max_borrow * cfg.opt("elyra", "elyra_borrow_elyra_share", 40.0) / 100) - borrowed
    if extra < 10**6:  # меньше 1 USDC: позиции под $1 поинтов не дают
        log.info("%s: elyra: займ под ELYRA не нужен (взято %s USDC, лимит %s)", tag, fmt_units(borrowed, 6),
                 fmt_units(max_borrow, 6))
        return
    link = send_tx(E, acc, POOL, pool.encode_abi("borrow", args=[MARKET_ELYRA_COLLATERAL, extra, 0, me, me]))
    log.info("%s: elyra: займ %s USDC под залог ELYRA (лимит %s), %s", tag, fmt_units(extra, 6),
             fmt_units(max_borrow, 6), link)
    pause(cfg)
    if not cfg.opt("elyra", "elyra_borrow_elyra_resupply", 1):
        return
    ensure_allowance(E, acc, USDC, POOL, extra, tag, "elyra")
    link = send_tx(E, acc, POOL, pool.encode_abi("supply", args=[MARKET_USDC_LOAN, extra, 0, me, b""]))
    log.info("%s: elyra: занятые %s USDC — в депозит (рынок займа USDC), %s", tag, fmt_units(extra, 6), link)
    pause(cfg)


def run(acc: dict, cfg: Config, tag: str) -> None:
    if not cfg.opt("elyra", "elyra", 0):
        return
    me = acc["address"]
    pool = contract(E, POOL, POOL_ABI, acc)
    gw = contract(E, GATEWAY, GATEWAY_ABI, acc)

    if not call(pool.functions.isAuthorized(me, cs(GATEWAY))):
        link = send_tx(E, acc, POOL, pool.encode_abi("setAuthorization", args=[cs(GATEWAY), True]))
        log.info("%s: elyra: разрешение для HYPE-gateway, %s", tag, link)
        pause(cfg)

    lo, hi = cfg.opt("elyra", "supply_hype_min", 0.0005), cfg.opt("elyra", "supply_hype_max", 0.001)
    keep = cfg.opt("elyra", "supply_hype_keep", 0.0)
    amt = 0
    if keep > 0:
        # всё, что сверх рабочего запаса, — в Elyra: HYPE считается ×2 каждый час и не тратится
        have = with_retry(w3(E[0], acc).eth.get_balance, me)
        amt = have - int(keep * 10**18)
        if amt < int(max(lo, 0.015) * 10**18):  # позиции меньше $1 поинтов не дают
            log.info("%s: elyra: HYPE не вношу — на кошельке %s, запас %s", tag, fmt(have), fmt(int(keep * 10**18)))
            amt = 0
    elif hi > 0:
        amt = rand_amount(lo, hi)
    if amt:
        link = send_tx(E, acc, GATEWAY, gw.encode_abi("supplyHYPE", args=[MARKET_WHYPE_LOAN, me]), value=amt)
        log.info("%s: elyra: депозит %s HYPE (рынок займа WHYPE), %s", tag, fmt(amt), link)
        pause(cfg)

    share = cfg.opt("elyra", "supply_usdc_share", 50.0) / 100
    usdc = int(erc20_balance(E, USDC, me, acc) * share)
    if usdc >= 10**5:  # от 0.1 USDC
        ensure_allowance(E, acc, USDC, POOL, usdc, tag, "elyra")
        link = send_tx(E, acc, POOL, pool.encode_abi("supply", args=[MARKET_USDC_LOAN, usdc, 0, me, b""]))
        log.info("%s: elyra: депозит %s USDC (рынок займа USDC), %s", tag, fmt_units(usdc, 6), link)
        pause(cfg)

    if cfg.opt("elyra", "elyra_collateral", 1):
        # весь ELYRA с фосета — залогом в рынок ELYRA → USDC, без займа: поинты ×1.5 (ELYRA по оракулу $0.10)
        elyra_bal = erc20_balance(E, ELYRA, me, acc)
        if elyra_bal >= 10**18:  # от 1 ELYRA
            ensure_allowance(E, acc, ELYRA, POOL, elyra_bal, tag, "elyra")
            link = send_tx(E, acc, POOL, pool.encode_abi(
                "supplyCollateral", args=[MARKET_ELYRA_COLLATERAL, elyra_bal, me, b""]))
            log.info("%s: elyra: залог %s ELYRA (без займа), %s", tag, fmt(elyra_bal), link)
            pause(cfg)

    if cfg.opt("elyra", "elyra_borrow_elyra", 0):
        borrow_against_elyra(acc, cfg, tag, pool)

    if not cfg.opt("elyra", "elyra_borrow", 0):
        return
    lo, hi = cfg.opt("elyra", "collateral_min", 0.001), cfg.opt("elyra", "collateral_max", 0.002)
    amt = rand_amount(lo, hi)
    link = send_tx(E, acc, GATEWAY, gw.encode_abi("supplyCollateralHYPE", args=[MARKET_USDC_LOAN, me]), value=amt)
    log.info("%s: elyra: залог %s HYPE, %s", tag, fmt(amt), link)
    pause(cfg)
    pos = call(contract(E, LENS, LENS_ABI, acc).functions.positionView(MARKET_USDC_LOAN, me))
    borrowed, max_borrow = pos[2], pos[6]
    # maxBorrow * доля минус уже взятое: займ остаётся далеко от ликвидации
    extra = int(max_borrow * cfg.opt("elyra", "borrow_share", 25.0) / 100) - borrowed
    if extra < 10**4:  # меньше 0.01 USDC — не занимаем
        log.info("%s: elyra: займ не нужен (взято %s USDC, лимит %s)", tag, fmt_units(borrowed, 6),
                 fmt_units(max_borrow, 6))
        return
    link = send_tx(E, acc, POOL, pool.encode_abi("borrow", args=[MARKET_USDC_LOAN, extra, 0, me, me]))
    log.info("%s: elyra: займ %s USDC под залог HYPE, %s", tag, fmt_units(extra, 6), link)
