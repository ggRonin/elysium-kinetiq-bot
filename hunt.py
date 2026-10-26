"""Elysium Hunt (elysium.archuntx.site) — карточная игра с ежедневными квестами за EXP.

Квест засчитывается так: сервер игры подписывает (POST /api/sign-quest {account, questId}) →
подпись отправляется в контракт квестов claimQuest(questId, nonce, signature).
Ежедневные квесты (раз в сутки по UTC):
    4 — Daily Check-in, +100 EXP (карта не нужна);
    5 — Elysium Testnet Transaction, +250 EXP (нужна карта: сервер иначе отказывает).
    3 — Follow @archuntx, +1000 EXP один раз: X-ник из x_handles.txt отправляется на проверку
        (POST /api/social/submit), команда игры проверяет подписку, после одобрения бот клеймит квест.
Апгрейд карт (hunt_upgrade = 1): за успешный апгрейд дают EXP, и он же засчитывает квест 5.
    upgradeRank(карта) с комиссией → через 5 блоков revealUpgrade(карта) (окно 250 блоков) → уровень вырос или нет.
    Шанс успеха меньше 100%: при неудаче — повтор (hunt_upgrade_tries), если кулдаун карты позволяет.
    После апгрейда у карты кулдаун (upgradeCooldown, 1 ч) — следующий при следующем проходе.
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
    {"type": "function", "name": "hasClaimed", "stateMutability": "view", "inputs": [_A("a"), _U("q")],
     "outputs": [{"type": "bool"}]},
    {"type": "function", "name": "lastClaimedDay", "stateMutability": "view", "inputs": [_A("a"), _U("q")],
     "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "quests", "stateMutability": "view", "inputs": [_U("q")],
     "outputs": [{"name": "expReward", "type": "uint256"}, {"name": "limitType", "type": "uint8"},
                 {"name": "isActive", "type": "bool"}]},
]
CORE_ABI = [
    {"type": "function", "name": "bountyHunterOfOwnerByIndex", "stateMutability": "view",
     "inputs": [_A("o"), _U("i")], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "getBountyHunter", "stateMutability": "view", "inputs": [_U("id")],
     "outputs": [{"type": "tuple", "components": [
         {"name": "rank", "type": "uint8"}, _U("lastHarvestTimestamp"), _U("lastUpgradeTimestamp"),
         {"name": "isEligibleForUpgrade", "type": "bool"}, {"name": "isUpgrading", "type": "bool"},
         {"name": "isTransferred", "type": "bool"}, {"name": "hasWithdrawn", "type": "bool"}]}]},
    {"type": "function", "name": "upgradeRank", "stateMutability": "payable", "inputs": [_U("id")], "outputs": []},
    {"type": "function", "name": "revealUpgrade", "stateMutability": "nonpayable", "inputs": [_U("id")], "outputs": []},
    {"type": "function", "name": "unlockLevelingUp", "stateMutability": "nonpayable", "inputs": [_U("requestId")],
     "outputs": []},
    {"type": "function", "name": "pendingUpgradeRequest", "stateMutability": "view", "inputs": [_U("id")],
     "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "upgradeCooldown", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "getSeasonFactoryAddress", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "address"}]},
    {"type": "function", "name": "getQuantumRNGAddress", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "address"}]},
    {"type": "function", "name": "balanceOf", "stateMutability": "view", "inputs": [_A("a")],
     "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "PUBLIC_PRICE", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "publicSaleStatus", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "bool"}]},
    {"type": "function", "name": "publicMint", "stateMutability": "payable", "inputs": [_U("qty")], "outputs": []},
]


FEE_ABI = [{"type": "function", "name": "getLevelUpFee", "stateMutability": "view",
            "inputs": [{"name": "currentRank", "type": "uint8"}], "outputs": [{"type": "uint256"}]}]
QRNG_ABI = [
    {"type": "function", "name": "getQuantumRequest", "stateMutability": "view", "inputs": [_U("id")],
     "outputs": [{"type": "tuple", "components": [
         {"name": "fulfilled", "type": "bool"}, {"name": "exists", "type": "bool"}, _A("contractAddress"),
         _U("tokenId"), _U("randomNumber"), _U("requestTime"), _U("commitBlock")]}]},
    {"type": "function", "name": "REVEAL_DELAY", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "REVEAL_WINDOW", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "uint256"}]},
]
RANK_NAMES = {1: "Scavenger", 2: "Mercenary", 3: "Vanguard", 4: "Apex"}


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


def _site(acc: dict, method: str, path: str, **kw):
    """Запрос к серверу игры: через прокси аккаунта, если не отвечает — напрямую."""
    for proxy in dict.fromkeys([acc.get("proxy"), None]):
        try:
            r = http.request(method, SITE + path, proxy=proxy, impersonate="chrome136", timeout=30,
                             headers={"Origin": SITE, "Referer": SITE + "/quests"}, **kw)
            return r.json()
        except Exception as e:
            log.debug("hunt: %s %s через %s: %s", method, path, mask_proxy(proxy), e)
    return {"ok": False, "error": "сервер игры недоступен"}


def _claim(acc: dict, quests, qid: int, name: str, tag: str, cfg: Config) -> bool:
    sig = sign_quest(acc, qid, tag)
    if "signature" not in sig:
        log.info("%s: hunt: «%s» — сервер отказал: %s", tag, name, sig.get("error", sig))
        return False
    signature = bytes.fromhex(sig["signature"].removeprefix("0x"))
    exp = call(quests.functions.quests(qid))[0]
    link = send_tx(E, acc, QUESTS, quests.encode_abi("claimQuest", args=[qid, int(sig["nonce"]), signature]))
    log.info("%s: hunt: «%s» +%d EXP, %s", tag, name, exp, link)
    pause(cfg)
    return True


def social_quest(acc: dict, cfg: Config, tag: str, quests) -> None:
    """Квест 3 «Follow @archuntx» (+1000 EXP, один раз): отправить X-ник, после одобрения командой — клейм."""
    from common import x_handle
    if call(quests.functions.hasClaimed(acc["address"], 3)):
        return
    handle = x_handle(acc)
    if not handle:
        log.info("%s: hunt: квест X — нет ника в x_handles.txt для этого аккаунта", tag)
        return
    st = _site(acc, "GET", "/api/social/status", params={"account": acc["address"]})
    status = st.get("status") or "none"
    if status in ("none", "unknown", "rejected"):
        r = _site(acc, "POST", "/api/social/submit", json={"account": acc["address"], "handle": handle})
        if not r.get("ok"):
            log.info("%s: hunt: квест X — ник @%s не принят: %s", tag, handle, r.get("error"))
            return
        status = r.get("status") or "pending"
        log.info("%s: hunt: квест X — ник @%s отправлен на проверку (%s)", tag, handle, status)
    if status != "approved":
        log.info("%s: hunt: квест X — @%s ждёт проверки подписки на @archuntx командой игры", tag, handle)
        return
    _claim(acc, quests, 3, "Follow @archuntx", tag, cfg)


MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"


def _chain_block(w3) -> int:
    """block.number так, как его видят контракты. В Elysium (Arbitrum Orbit) это номер блока HyperEVM,
    а не номер блока Elysium из eth_blockNumber — по нему считаются commitBlock и окно раскрытия."""
    from web3 import Web3
    r = w3.eth.call({"to": MULTICALL3, "data": "0x" + Web3.keccak(text="getBlockNumber()")[:4].hex()})
    return int.from_bytes(r, "big")


def _wait_block(w3, target: int, timeout: float = 120) -> None:
    end = time.time() + timeout
    while time.time() < end and _chain_block(w3) < target:
        time.sleep(1)


def upgrade_cards(acc: dict, cfg: Config, tag: str, core) -> int:
    """Апгрейд всех карт аккаунта: до одного удачного апгрейда на карту за проход. Возвращает число попыток."""
    from bridge import w3 as get_w3
    if not cfg.opt("hunt", "hunt_upgrade", 0):
        return 0
    me = acc["address"]
    w3 = get_w3(E[0], acc)
    max_rank = cfg.opt("hunt", "hunt_upgrade_to", 3)
    max_fee = int(cfg.opt("hunt", "hunt_upgrade_max_fee", 0.03) * 10**18)
    tries = cfg.opt("hunt", "hunt_upgrade_tries", 3)
    fees = contract(E, call(core.functions.getSeasonFactoryAddress()), FEE_ABI, acc)
    qrng = contract(E, call(core.functions.getQuantumRNGAddress()), QRNG_ABI, acc)
    delay = call(qrng.functions.REVEAL_DELAY())
    window = call(qrng.functions.REVEAL_WINDOW())
    cooldown = call(core.functions.upgradeCooldown())
    ids = [call(core.functions.bountyHunterOfOwnerByIndex(me, i)) for i in range(call(core.functions.balanceOf(me)))]
    attempts = 0

    def reveal(card: int) -> None:
        for _ in range(8):
            try:
                send_tx(E, acc, CORE, core.encode_abi("revealUpgrade", args=[card]))
                return
            except Exception as e:
                t = str(e)
                if "RevealTooEarly" in t or "0xc167e8fa" in t or "reveal is not" in t.lower():
                    time.sleep(2)
                    continue
                raise

    for card in ids:
        rank, _, last_up, _, upgrading, _, _ = call(core.functions.getBountyHunter(card))
        if upgrading:  # висит незавершённый апгрейд: раскрыть или освободить карту
            req = call(core.functions.pendingUpgradeRequest(card))
            q = call(qrng.functions.getQuantumRequest(req))
            if _chain_block(w3) <= q[6] + window:
                _wait_block(w3, q[6] + delay + 1)
                reveal(card)
                log.info("%s: hunt: карта #%d — раскрыт незавершённый апгрейд", tag, card)
            elif time.time() > q[5] + 1800:
                send_tx(E, acc, CORE, core.encode_abi("unlockLevelingUp", args=[req]))
                log.info("%s: hunt: карта #%d — окно раскрытия прошло, карта освобождена", tag, card)
            else:
                log.info("%s: hunt: карта #%d — окно раскрытия прошло, освободится через ~%d мин", tag, card,
                         (q[5] + 1800 - time.time()) // 60 + 1)
            continue
        for attempt in range(1, tries + 1):
            rank, _, last_up, _, _, _, _ = call(core.functions.getBountyHunter(card))
            if rank >= max_rank:
                break
            if rank > 1 and last_up + cooldown > time.time():
                log.info("%s: hunt: карта #%d (%s) — кулдаун ещё %d мин", tag, card, RANK_NAMES.get(rank, rank),
                         (last_up + cooldown - time.time()) // 60 + 1)
                break
            fee = call(fees.functions.getLevelUpFee(rank))
            if fee > max_fee:
                log.info("%s: hunt: карта #%d — апгрейд стоит %s HYPE, больше hunt_upgrade_max_fee", tag, card,
                         fmt(fee))
                break
            link = send_tx(E, acc, CORE, core.encode_abi("upgradeRank", args=[card]), value=fee)
            attempts += 1
            req = call(core.functions.pendingUpgradeRequest(card))
            commit = call(qrng.functions.getQuantumRequest(req))[6]
            _wait_block(w3, commit + delay + 1)
            reveal(card)
            new_rank = call(core.functions.getBountyHunter(card))[0]
            if new_rank > rank:
                log.info("%s: hunt: карта #%d — апгрейд удался: %s → %s (попытка %d, %s HYPE), %s", tag, card,
                         RANK_NAMES.get(rank, rank), RANK_NAMES.get(new_rank, new_rank), attempt, fmt(fee), link)
                break
            log.info("%s: hunt: карта #%d — апгрейд не удался (попытка %d/%d, шанс не сыграл)", tag, card,
                     attempt, tries)
            pause(cfg)
        pause(cfg)
    return attempts


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

    if cards:
        upgrade_cards(acc, cfg, tag, core)  # до квестов: апгрейд засчитывает «Elysium Testnet Transaction»
    if cfg.opt("hunt", "hunt_x_quest", 0):
        social_quest(acc, cfg, tag, quests)

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
        _claim(acc, quests, qid, name, tag, cfg)


# ── EHUNT: разово купить 50–63 EHUNT и застейкать (Flexible, без лока) ──────
EHUNT = "0x4C0Ba5e208734ACF8DdC127BE10E5DCA6F01cE92"
HUNT_ROUTER = "0xBD0243B0A336C4316fBD0C01699178e95883DE2c"   # UniswapV2-роутер игры (пул EHUNT/HYPE)
HUNT_WHYPE = "0xf4ee3103c2f380be0364a4B87a7C6A8B2cD3B1e4"    # их wrapped HYPE для пути свопа
STAKING = "0x14f3C17481EFDEc544458710cd092A167Aeafb0b"        # ElyHuntStaking, минимум 50 EHUNT
_PATH = {"name": "path", "type": "address[]"}
ROUTER_ABI = [
    {"type": "function", "name": "getAmountsIn", "stateMutability": "view",
     "inputs": [_U("amountOut"), _PATH], "outputs": [{"type": "uint256[]"}]},
    {"type": "function", "name": "swapETHForExactTokens", "stateMutability": "payable",
     "inputs": [_U("amountOut"), _PATH, _A("to"), _U("deadline")], "outputs": [{"type": "uint256[]"}]},
]
STAKING_ABI = [
    {"type": "function", "name": "getUserPositionCount", "stateMutability": "view", "inputs": [_A("a")],
     "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "minStakeAmount", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "stake", "stateMutability": "nonpayable", "inputs": [_U("tierId"), _U("amount")],
     "outputs": []},
]


def ehunt_stake(acc: dict, cfg: Config, tag: str) -> None:
    """Один раз на аккаунт: купить случайно ehunt_min–ehunt_max EHUNT за HYPE и застейкать в Flexible.
    Если покупка дороже ehunt_max_share % всех HYPE аккаунта (Elysium + HyperEVM) — пропуск."""
    import random
    from bridge import w3 as get_w3
    from onchain import H, cs, ensure_allowance, erc20_balance
    if not cfg.opt("hunt", "ehunt_stake", 0):
        return
    me = acc["address"]
    st = contract(E, STAKING, STAKING_ABI, acc)
    if call(st.functions.getUserPositionCount(me)) > 0:
        log.info("%s: hunt: EHUNT уже застейкан — пропуск", tag)
        return
    min_stake = call(st.functions.minStakeAmount())
    lo, hi = cfg.opt("hunt", "ehunt_min", 50.0), cfg.opt("hunt", "ehunt_max", 63.0)
    want = max(int(round(random.uniform(lo, hi), random.randint(1, 4)) * 10**18), min_stake)
    have = erc20_balance(E, EHUNT, me, acc)
    if have < want:
        r = contract(E, HUNT_ROUTER, ROUTER_ABI, acc)
        path = [cs(HUNT_WHYPE), cs(EHUNT)]
        need = want - have
        cost = call(r.functions.getAmountsIn(need, path))[0]
        value = int(cost * 1.03)  # запас на движение цены; лишнее роутер вернёт
        bal_e = get_w3(E[0], acc).eth.get_balance(me)
        try:
            bal_h = get_w3(H[0], acc).eth.get_balance(me)
        except Exception:
            bal_h = 0
        share = value / max(bal_e + bal_h, 1)
        if share > cfg.opt("hunt", "ehunt_max_share", 30.0) / 100:
            log.info("%s: hunt: EHUNT пропущен — %s EHUNT стоят %s HYPE, это %.0f%% всех HYPE аккаунта "
                        "(Elysium %s + HyperEVM %s), больше %s%%", tag, fmt(need), fmt(cost), share * 100,
                        fmt(bal_e), fmt(bal_h), cfg.opt("hunt", "ehunt_max_share", 30.0))
            return
        if value + 10**16 > bal_e:
            log.info("%s: hunt: EHUNT — на Elysium мало HYPE (%s, нужно ~%s), пропуск", tag, fmt(bal_e), fmt(value))
            return
        link = send_tx(E, acc, HUNT_ROUTER, r.encode_abi(
            "swapETHForExactTokens", args=[need, path, me, int(time.time()) + 600]), value=value)
        log.info("%s: hunt: куплено %s EHUNT за ~%s HYPE, %s", tag, fmt(need), fmt(cost), link)
        pause(cfg)
    amount = min(erc20_balance(E, EHUNT, me, acc), want)
    if amount < min_stake:
        log.info("%s: hunt: EHUNT %s — меньше минимума стейка %s", tag, fmt(amount), fmt(min_stake))
        return
    ensure_allowance(E, acc, EHUNT, STAKING, amount, tag, "hunt EHUNT")
    link = send_tx(E, acc, STAKING, st.encode_abi("stake", args=[0, amount]))
    log.info("%s: hunt: застейкано %s EHUNT (Flexible, без лока), %s", tag, fmt(amount), link)
