"""Wrap HYPE → WHYPE на Elysium (deposit) и обратно (withdraw).

WHYPE на Elysium — 0xCD57…F234, тот же токен, что приходит через бридж с HyperEVM (0x5555…5555).
[wrap] wrap = 0 — пропуск, 1 — только завернуть, 2 — завернуть и сразу развернуть ту же сумму.
"""
from __future__ import annotations

from common import Config, log
from onchain import E, contract, fmt, pause, rand_amount, send_tx

WHYPE = "0xCD57F65c2B0e5881CFC2E609F7CD53b746E1F234"
ABI = [
    {"type": "function", "name": "deposit", "stateMutability": "payable", "inputs": [], "outputs": []},
    {"type": "function", "name": "withdraw", "stateMutability": "nonpayable",
     "inputs": [{"name": "wad", "type": "uint256"}], "outputs": []},
]


def run(acc: dict, cfg: Config, tag: str) -> None:
    mode = cfg.opt("wrap", "wrap", 0)
    if not mode:
        return
    lo, hi = cfg.opt("wrap", "wrap_min", 0.0001), cfg.opt("wrap", "wrap_max", 0.0003)
    rounds = cfg.opt("wrap", "wrap_rounds", 1)
    w = contract(E, WHYPE, ABI, acc)
    for i in range(1, rounds + 1):
        amt = rand_amount(lo, hi)
        link = send_tx(E, acc, WHYPE, w.encode_abi("deposit"), value=amt)
        log.info("%s: wrap %d/%d: %s HYPE → WHYPE, %s", tag, i, rounds, fmt(amt), link)
        if mode == 2:
            pause(cfg)
            link = send_tx(E, acc, WHYPE, w.encode_abi("withdraw", args=[amt]))
            log.info("%s: wrap %d/%d: %s WHYPE → HYPE, %s", tag, i, rounds, fmt(amt), link)
        if i < rounds:
            pause(cfg)
