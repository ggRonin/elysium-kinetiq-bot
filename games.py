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


FROG_NAMES = "0x8463EC335192B4b0E5cc88ebF35e1B1121C875f3"  # ники игроков Frog Flip (бесплатно, только газ)
FROG_NAMES_ABI = [
    {"type": "function", "name": "nameOf", "stateMutability": "view", "inputs": [{"name": "a", "type": "address"}],
     "outputs": [{"type": "string"}]},
    {"type": "function", "name": "isAvailable", "stateMutability": "view", "inputs": [{"name": "n", "type": "string"}],
     "outputs": [{"type": "bool"}]},
    {"type": "function", "name": "setName", "stateMutability": "nonpayable", "inputs": [{"name": "n", "type": "string"}],
     "outputs": []},
]
_SYL = ["ka", "ri", "no", "ve", "lu", "ta", "mi", "so", "ze", "ra", "do", "ny", "el", "ar", "on", "is", "ky", "ma",
        "xo", "li", "fe", "bo", "tor", "vin", "dex", "lyn", "mor", "zen", "frog", "hop"]


def _nick() -> str:
    """Случайный ник по правилам Frog Flip: 3–16 символов, латиница, цифры, _."""
    name = "".join(random.choice(_SYL) for _ in range(random.randint(2, 3)))
    if random.random() < 0.4:
        name = name.capitalize()
    if random.random() < 0.3:
        name += "_"
    if random.random() < 0.6:
        name += str(random.randint(1, 999))
    return name[:16]


def frog_name(acc: dict, cfg: Config, tag: str) -> None:
    """Один раз задать аккаунту ник в Frog Flip, если его ещё нет."""
    if not cfg.opt("games", "frog_name", 0):
        return
    c = contract(E, FROG_NAMES, FROG_NAMES_ABI, acc)
    current = call(c.functions.nameOf(acc["address"]))
    if current:
        log.info("%s: frogflip: ник уже есть — %s", tag, current)
        return
    for _ in range(10):
        nick = _nick()
        if len(nick) >= 3 and call(c.functions.isAvailable(nick)):
            break
    else:
        log.warning("%s: frogflip: не нашёл свободный ник за 10 попыток", tag)
        return
    link = send_tx(E, acc, FROG_NAMES, c.encode_abi("setName", args=[nick]))
    log.info("%s: frogflip: ник %s, %s", tag, nick, link)


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
