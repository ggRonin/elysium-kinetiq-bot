"""Своп HYPE ↔ USDC на Elysium через UniswapV2-роутер.

KyberSwap Elysium testnet не поддерживает, у HexSwap нет ликвидности, у Uniswap V3 нет пула WHYPE/USDC.
Живой пул — UniswapV2-форк: роутер 0x72D4…94c7, пара WETH(0xff56…, его собственный wrapped HYPE)/USDC.

[swap] swap = 0 — пропуск, 1 — HYPE → USDC, 2 — HYPE → USDC и весь полученный USDC обратно в HYPE.
"""
from __future__ import annotations

import random
import time

from common import Config, log
from onchain import (E, call, contract, cs, ensure_allowance, erc20_balance, fmt, fmt_units, pause,
                     rand_amount, send_tx)

ROUTER = "0x72D435b8030A5195Bdb7594EDEa1ff315B9994c7"
WETH = "0xff5681eB95bBACfbB334F80FcdBC31Fb59F22332"  # wrapped HYPE этого роутера (не WHYPE 0xCD57)
USDC = "0x7D29d8047B905000459c0E80c34a26CeedcB47b2"
USDC_DEC = 6

_addr_list = {"name": "path", "type": "address[]"}
ABI = [
    {"type": "function", "name": "getAmountsOut", "stateMutability": "view",
     "inputs": [{"name": "amountIn", "type": "uint256"}, _addr_list], "outputs": [{"type": "uint256[]"}]},
    {"type": "function", "name": "getAmountsIn", "stateMutability": "view",
     "inputs": [{"name": "amountOut", "type": "uint256"}, _addr_list], "outputs": [{"type": "uint256[]"}]},
    {"type": "function", "name": "swapETHForExactTokens", "stateMutability": "payable",
     "inputs": [{"name": "amountOut", "type": "uint256"}, _addr_list, {"name": "to", "type": "address"},
                {"name": "deadline", "type": "uint256"}], "outputs": [{"type": "uint256[]"}]},
    {"type": "function", "name": "swapExactETHForTokens", "stateMutability": "payable",
     "inputs": [{"name": "amountOutMin", "type": "uint256"}, _addr_list, {"name": "to", "type": "address"},
                {"name": "deadline", "type": "uint256"}], "outputs": [{"type": "uint256[]"}]},
    {"type": "function", "name": "swapExactTokensForETH", "stateMutability": "nonpayable",
     "inputs": [{"name": "amountIn", "type": "uint256"}, {"name": "amountOutMin", "type": "uint256"}, _addr_list,
                {"name": "to", "type": "address"}, {"name": "deadline", "type": "uint256"}],
     "outputs": [{"type": "uint256[]"}]},
]


def _min_out(r, amount: int, path: list[str], slippage: float) -> int:
    out = call(r.functions.getAmountsOut(amount, path))[-1]
    return int(out * (1 - slippage))


def _send_swap(acc: dict, build, value: int = 0) -> str:
    """Отправить своп; если цена ушла (пул общий, аккаунты меняют одновременно) — пересчитать минимум
    по свежей цене с увеличенным допуском и повторить, до 3 попыток. build(mult) → calldata."""
    for attempt in range(3):
        try:
            return send_tx(E, acc, ROUTER, build(attempt + 1), value=value)
        except Exception as e:
            t = str(e)
            if attempt == 2 or not ("INSUFFICIENT_OUTPUT" in t or "EXCESSIVE_INPUT" in t or "отклонена" in t):
                raise
            time.sleep(random.uniform(2, 5))


def buy_usdc(acc: dict, amount: int, tag: str, slippage: float = 0.03) -> str:
    """Купить ровно amount USDC за HYPE (лишний HYPE роутер вернёт)."""
    r = contract(E, ROUTER, ABI, acc)
    path = [cs(WETH), cs(USDC)]
    cost = call(r.functions.getAmountsIn(amount, path))[0]
    value = int(cost * (1 + slippage * 2))  # лишнее роутер вернёт
    link = _send_swap(acc, lambda m: r.encode_abi(
        "swapETHForExactTokens", args=[amount, path, acc["address"], int(time.time()) + 600]), value=value)
    log.info("%s: swap: куплено %s USDC за ~%s HYPE, %s", tag, fmt_units(amount, USDC_DEC), fmt(cost), link)
    return link


def run(acc: dict, cfg: Config, tag: str) -> None:
    mode = cfg.opt("swap", "swap", 0)
    if not mode:
        return
    lo, hi = cfg.opt("swap", "swap_min", 0.0001), cfg.opt("swap", "swap_max", 0.0003)
    rounds = cfg.opt("swap", "swap_rounds", 1)
    slippage = cfg.opt("swap", "slippage", 3.0) / 100
    me = acc["address"]
    r = contract(E, ROUTER, ABI, acc)
    buy, sell = [cs(WETH), cs(USDC)], [cs(USDC), cs(WETH)]
    for i in range(1, rounds + 1):
        amt = rand_amount(lo, hi)
        before = erc20_balance(E, USDC, me, acc)
        link = _send_swap(acc, lambda m: r.encode_abi(
            "swapExactETHForTokens", args=[_min_out(r, amt, buy, slippage * m), buy, me, int(time.time()) + 600]),
            value=amt)
        got = erc20_balance(E, USDC, me, acc) - before
        log.info("%s: swap %d/%d: %s HYPE → %s USDC, %s", tag, i, rounds, fmt(amt), fmt_units(got, USDC_DEC), link)
        if mode == 2 and got > 0:
            pause(cfg)
            ensure_allowance(E, acc, USDC, ROUTER, got, tag, "swap")
            link = _send_swap(acc, lambda m: r.encode_abi(
                "swapExactTokensForETH",
                args=[got, _min_out(r, got, sell, slippage * m), sell, me, int(time.time()) + 600]))
            log.info("%s: swap %d/%d: %s USDC → HYPE, %s", tag, i, rounds, fmt_units(got, USDC_DEC), link)
        if i < rounds:
            pause(cfg)
