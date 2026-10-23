"""Elysium Hunt (elysium.archuntx.site) — карточная игра с ежедневными квестами за EXP.

Квест засчитывается так: сервер игры подписывает (POST /api/sign-quest {account, questId}) →
подпись отправляется в контракт квестов claimQuest(questId, nonce, signature).
Ежедневные квесты (раз в сутки по UTC):
    4 — Daily Check-in, +100 EXP (карта не нужна);
    5 — Elysium Testnet Transaction, +250 EXP (нужна карта: сервер иначе отказывает).
Квесты с X/Telegram/репостом бот не трогает.
hunt_mint = 1 — если карты нет, сминтить одну (цена из контракта, сейчас 0.05 HYPE).
"""
from __future__ import annotations

import time

from curl_cffi import requests as http

from common import Config, log, mask_proxy
from onchain import E, call, contract, fmt, pause, send_tx

SITE = "https://elysium.archuntx.site"
QUESTS = "0xfF829934Db2aa13e48478056843e78585648f146"
CORE = "0x896b17b15C7acB6Dcde257d07f4Ab3628Bbc5b52"
DAILY = {4: "Daily Check-in", 5: "Elysium Testnet Transaction"}
NEEDS_CARD = {5}

_A = lambda n: {"name": n, "type": "address"}
_U = lambda n: {"name": n, "type": "uint256"}
QUESTS_ABI = [
    {"type": "function", "name": "claimQuest", "stateMutability": "nonpayable",
     "inputs": [_U("questId"), _U("nonce"), {"name": "signature", "type": "bytes"}], "outputs": []},
    {"type": "function", "name": "lastClaimedDay", "stateMutability": "view", "inputs": [_A("a"), _U("q")],
     "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "quests", "stateMutability": "view", "inputs": [_U("q")],
     "outputs": [{"name": "expReward", "type": "uint256"}, {"name": "limitType", "type": "uint8"},
                 {"name": "isActive", "type": "bool"}]},
]
CORE_ABI = [
    {"type": "function", "name": "balanceOf", "stateMutability": "view", "inputs": [_A("a")],
     "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "PUBLIC_PRICE", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "publicSaleStatus", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "bool"}]},
    {"type": "function", "name": "publicMint", "stateMutability": "payable", "inputs": [_U("qty")], "outputs": []},
]


def sign_quest(acc: dict, quest_id: int, tag: str) -> dict:
    """Подпись квеста от сервера игры. Через прокси аккаунта; если прокси не отвечает — напрямую."""
    body = {"account": acc["address"], "questId": quest_id}
    for proxy in dict.fromkeys([acc.get("proxy"), None]):
        try:
            r = http.post(SITE + "/api/sign-quest", json=body, proxy=proxy, impersonate="chrome136", timeout=30,
                          headers={"Origin": SITE, "Referer": SITE + "/"})
        except Exception as e:
            log.debug("%s: hunt: sign-quest через %s: %s", tag, mask_proxy(proxy), e)
            continue
        try:
            return r.json()
        except ValueError:
            return {"error": f"HTTP {r.status_code}"}
    return {"error": "сервер игры недоступен"}


def run(acc: dict, cfg: Config, tag: str) -> None:
    if not cfg.opt("hunt", "hunt", 0):
        return
    me = acc["address"]
    core = contract(E, CORE, CORE_ABI, acc)
    quests = contract(E, QUESTS, QUESTS_ABI, acc)
    cards = call(core.functions.balanceOf(me))

    if not cards and cfg.opt("hunt", "hunt_mint", 0):
        price = call(core.functions.PUBLIC_PRICE())
        max_price = int(cfg.opt("hunt", "hunt_max_price", 0.05) * 10**18)
        if not call(core.functions.publicSaleStatus()):
            log.info("%s: hunt: минт закрыт", tag)
        elif price > max_price:
            log.info("%s: hunt: карта стоит %s HYPE, больше hunt_max_price — пропуск", tag, fmt(price))
        else:
            link = send_tx(E, acc, CORE, core.encode_abi("publicMint", args=[1]), value=price)
            log.info("%s: hunt: сминтил карту за %s HYPE, %s", tag, fmt(price), link)
            cards = 1
            pause(cfg)

    today = int(time.time()) // 86400
    for qid, name in DAILY.items():
        if qid in NEEDS_CARD and not cards:
            continue
        exp, _, active = call(quests.functions.quests(qid))
        if not active:
            continue
        if call(quests.functions.lastClaimedDay(me, qid)) == today:
            log.info("%s: hunt: «%s» сегодня уже засчитан", tag, name)
            continue
        sig = sign_quest(acc, qid, tag)
        if "signature" not in sig:
            log.info("%s: hunt: «%s» — сервер отказал: %s", tag, name, sig.get("error", sig))
            continue
        signature = bytes.fromhex(sig["signature"].removeprefix("0x"))
        data = quests.encode_abi("claimQuest", args=[qid, int(sig["nonce"]), signature])
        link = send_tx(E, acc, QUESTS, data)
        log.info("%s: hunt: «%s» +%d EXP, %s", tag, name, exp, link)
        pause(cfg)
