""".ely Name Service (elysiumnameservice.xyz): регистрация имени на аккаунт и назначение его основным.

Регистрация в два шага (защита от перехвата имени): commit(хэш) → подождать ≥15 с → register(... value=цена).
Цена за год: 2–3 символа 0.3 HYPE, 4–5 символов 0.2 HYPE, длиннее — 0.1 HYPE.
Если у аккаунта уже есть имя (primary или NFT), повторно не регистрирует.
"""
from __future__ import annotations

import os
import random
import time

from web3 import Web3

from common import Config, log
from onchain import E, call, contract, fmt, send_tx

NAMES = "0x13A130B336400409c956dF042C128238F1486ddD"
CTRL = "0xA434b4D98dc9e35205bf186B1750ed2961791F2d"
YEAR = 31_536_000
COMMIT_WAIT = 20  # с; контракт требует минимум 15

_S = lambda n: {"name": n, "type": "string"}
_A = lambda n: {"name": n, "type": "address"}
_U = lambda n: {"name": n, "type": "uint256"}
_B = lambda n: {"name": n, "type": "bytes32"}
NAMES_ABI = [
    {"type": "function", "name": "available", "stateMutability": "view", "inputs": [_S("l")], "outputs": [{"type": "bool"}]},
    {"type": "function", "name": "primaryNameOf", "stateMutability": "view", "inputs": [_A("a")],
     "outputs": [{"type": "string"}]},
    {"type": "function", "name": "balanceOf", "stateMutability": "view", "inputs": [_A("a")],
     "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "setPrimaryName", "stateMutability": "nonpayable", "inputs": [_U("id")], "outputs": []},
]
CTRL_ABI = [
    {"type": "function", "name": "registerPrice", "stateMutability": "view", "inputs": [_S("l"), _U("d"), _A("a")],
     "outputs": [{"type": "uint256"}, {"type": "uint16"}]},
    {"type": "function", "name": "isReserved", "stateMutability": "view", "inputs": [_S("l")], "outputs": [{"type": "bool"}]},
    {"type": "function", "name": "makeCommitment", "stateMutability": "pure", "inputs": [_S("l"), _A("o"), _B("s")],
     "outputs": [{"type": "bytes32"}]},
    {"type": "function", "name": "commit", "stateMutability": "nonpayable", "inputs": [_B("c")], "outputs": []},
    {"type": "function", "name": "register", "stateMutability": "payable",
     "inputs": [_S("l"), _A("o"), _U("d"), _B("s")], "outputs": []},
]

_SYL = ["ka", "ri", "no", "ve", "lu", "ta", "mi", "so", "ze", "ra", "do", "ny", "el", "ar", "on", "is", "ky", "ma",
        "xo", "li", "fe", "qu", "bo", "sha", "tor", "vin", "dex", "lyn", "mor", "zen"]


def random_label() -> str:
    """Похожее на ник имя от 7 символов: слоги + иногда цифры (самая дешёвая цена — от 6 символов)."""
    name = "".join(random.choice(_SYL) for _ in range(random.randint(3, 4)))
    if random.random() < 0.5:
        name += str(random.randint(1, 999))
    return name if len(name) >= 7 else name + str(random.randint(10, 99))


def run(acc: dict, cfg: Config, tag: str) -> None:
    if not cfg.opt("ely", "ely", 0):
        return
    me = acc["address"]
    names = contract(E, NAMES, NAMES_ABI, acc)
    ctrl = contract(E, CTRL, CTRL_ABI, acc)
    primary = call(names.functions.primaryNameOf(me))
    if primary:
        log.info("%s: .ely: уже есть имя %s", tag, primary if primary.endswith(".ely") else primary + ".ely")
        return
    if call(names.functions.balanceOf(me)) > 0:
        log.info("%s: .ely: имя уже зарегистрировано (не основное) — пропуск", tag)
        return

    max_price = Web3.to_wei(cfg.opt("ely", "ely_max_price", 0.1), "ether")
    for _ in range(10):
        label = random_label()
        if call(names.functions.available(label)) and not call(ctrl.functions.isReserved(label)):
            break
    else:
        log.warning("%s: .ely: не нашёл свободное имя за 10 попыток", tag)
        return
    price = call(ctrl.functions.registerPrice(label, YEAR, me))[0]
    if price > max_price:
        log.info("%s: .ely: %s.ely стоит %s HYPE, больше ely_max_price — пропуск", tag, label, fmt(price))
        return

    secret = os.urandom(32)
    commitment = call(ctrl.functions.makeCommitment(label, me, secret))
    link = send_tx(E, acc, CTRL, ctrl.encode_abi("commit", args=[commitment]))
    log.info("%s: .ely: резерв %s.ely, жду %d с, %s", tag, label, COMMIT_WAIT, link)
    time.sleep(COMMIT_WAIT)
    link = send_tx(E, acc, CTRL, ctrl.encode_abi("register", args=[label, me, YEAR, secret]), value=price)
    log.info("%s: .ely: зарегистрирован %s.ely за %s HYPE, %s", tag, label, fmt(price), link)

    token_id = int.from_bytes(Web3.keccak(text=label), "big")
    link = send_tx(E, acc, NAMES, names.encode_abi("setPrimaryName", args=[token_id]))
    log.info("%s: .ely: %s.ely — основное имя, %s", tag, label, link)
