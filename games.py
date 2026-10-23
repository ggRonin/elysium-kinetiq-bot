"""Игры на Elysium: одна небольшая ставка в день (результат решает казино, деньги тестовые).

ELYSA.FUN (elysa.fun): ElysaFlip.flip(side) с value; минимальная ставка 0.01 HYPE, раскрытие делает казино.
Frog Flip (frogflip.xyz): CoinFlip.placeBet(side, seedHash) с value; минимум 0.001 HYPE.
    seedHash — хэш случайного секрета игрока; результат раскрывает «дом», а если он не раскрыл
    за 6 часов, ставка засчитывается игроку как выигрыш.
"""
from __future__ import annotations

import os
import random

from web3 import Web3

from common import Config, log
from onchain import E, call, contract, fmt, send_tx

ELYSA_FLIP = "0xb68213Ec1088f6e62312da878f74E2E65Fc878Cf"
FROG_FLIP = "0x2CaE9BAc2D5b500aAA860CCe0A3A20d387F384d4"

ELYSA_ABI = [
    {"type": "function", "name": "flip", "stateMutability": "payable",
     "inputs": [{"name": "side", "type": "uint8"}], "outputs": []},
    {"type": "function", "name": "MIN_BET", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
]
FROG_ABI = [
    {"type": "function", "name": "placeBet", "stateMutability": "payable",
     "inputs": [{"name": "side", "type": "uint8"}, {"name": "seedHash", "type": "bytes32"}], "outputs": []},
    {"type": "function", "name": "minBet", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "maxBet", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
]


def elysa(acc: dict, cfg: Config, tag: str) -> None:
    if not cfg.opt("games", "elysa", 0):
        return
    g = contract(E, ELYSA_FLIP, ELYSA_ABI, acc)
    bet = max(int(cfg.opt("games", "elysa_bet", 0.01) * 10**18), call(g.functions.MIN_BET()))
    side = random.randint(0, 1)
    link = send_tx(E, acc, ELYSA_FLIP, g.encode_abi("flip", args=[side]), value=bet)
    log.info("%s: elysa: ставка %s HYPE на %s, %s", tag, fmt(bet), ("орла", "решку")[side], link)


def frogflip(acc: dict, cfg: Config, tag: str) -> None:
    if not cfg.opt("games", "frogflip", 0):
        return
    g = contract(E, FROG_FLIP, FROG_ABI, acc)
    lo, hi = call(g.functions.minBet()), call(g.functions.maxBet())
    # ставка должна быть кратна минимальной (0.001 HYPE), иначе BadStake()
    want = int(random.uniform(cfg.opt("games", "frog_min", 0.001), cfg.opt("games", "frog_max", 0.002)) * 10**18)
    bet = min(max(want // lo * lo, lo), hi // lo * lo)
    side = random.randint(0, 1)
    seed_hash = Web3.keccak(os.urandom(32))
    link = send_tx(E, acc, FROG_FLIP, g.encode_abi("placeBet", args=[side, seed_hash]), value=bet)
    log.info("%s: frogflip: ставка %s HYPE на %s, %s", tag, fmt(bet), ("орла", "решку")[side], link)
