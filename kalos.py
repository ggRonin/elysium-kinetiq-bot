"""Kalos (kalos.fun) — самый активный проект Elysium: DEX на Uniswap V3 (токен KALOS) и NFT-дропы через SeaDrop.

Поинты Kalos (сезон, лиги): минт NFT, вторичная торговля NFT, соцактивность, рефералы + ежедневный чек-ин
(пул поинтов дня делится между отметившимися). По поинтам за сутки раздаётся 1M KALOS в день — его надо забрать.
checkin():       чек-ин через их API (вход по подписи кошелька, без транзакции).
claim_rewards(): claimEpochs за дни, где есть несобранная награда.

swap(): HYPE → KALOS через SwapRouter Kalos (пул WHYPE/KALOS, комиссия 2%), при kalos_swap = 2 — весь KALOS обратно в HYPE.
nft():  публичный минт в активном дропе SeaDrop. Дроп ищется по свежим успешным mintPublic в эксплорере:
        окно минта открыто, цена ≤ nft_max_price, у аккаунта ещё нет NFT этой коллекции.
"""
from __future__ import annotations

import random
import time

from common import Config, log
from onchain import (E, call, contract, cs, ensure_allowance, erc20_balance, fmt, pause, rand_amount, recent_calls,
                     send_tx, simulate)

ROUTER = "0x752f6816Bc514e1Eeff852b2eAdcd96f6c0F9f3f"
WHYPE = "0xCD57F65c2B0e5881CFC2E609F7CD53b746E1F234"
KALOS = "0x8a2201953aF29E8bE7700BCb53Da3CE5CAD18726"
FEE = 20000  # 2% — пул WHYPE/KALOS с основной ликвидностью
SEADROP = "0xB3c542b227f732368AF5750a34641a8AA93DaEDB"

_P = {"name": "params", "type": "tuple", "components": [
    {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"}, {"name": "fee", "type": "uint24"},
    {"name": "recipient", "type": "address"}, {"name": "deadline", "type": "uint256"},
    {"name": "amountIn", "type": "uint256"}, {"name": "amountOutMinimum", "type": "uint256"},
    {"name": "sqrtPriceLimitX96", "type": "uint160"}]}
ROUTER_ABI = [
    {"type": "function", "name": "exactInputSingle", "stateMutability": "payable", "inputs": [_P],
     "outputs": [{"name": "amountOut", "type": "uint256"}]},
    {"type": "function", "name": "multicall", "stateMutability": "payable",
     "inputs": [{"name": "data", "type": "bytes[]"}], "outputs": [{"name": "results", "type": "bytes[]"}]},
    {"type": "function", "name": "unwrapWETH9", "stateMutability": "payable",
     "inputs": [{"name": "amountMinimum", "type": "uint256"}, {"name": "recipient", "type": "address"}], "outputs": []},
]
SEADROP_ABI = [
    {"type": "function", "name": "mintPublic", "stateMutability": "payable",
     "inputs": [{"name": "nftContract", "type": "address"}, {"name": "feeRecipient", "type": "address"},
                {"name": "minterIfNotPayer", "type": "address"}, {"name": "quantity", "type": "uint256"}],
     "outputs": []},
    {"type": "function", "name": "getPublicDrop", "stateMutability": "view",
     "inputs": [{"name": "nftContract", "type": "address"}],
     "outputs": [{"type": "tuple", "components": [
         {"name": "mintPrice", "type": "uint80"}, {"name": "startTime", "type": "uint48"},
         {"name": "endTime", "type": "uint48"}, {"name": "maxTotalMintableByWallet", "type": "uint16"},
         {"name": "feeBps", "type": "uint16"}, {"name": "restrictFeeRecipients", "type": "bool"}]}]},
]
NFT_ABI = [{"type": "function", "name": "balanceOf", "stateMutability": "view",
            "inputs": [{"name": "o", "type": "address"}], "outputs": [{"type": "uint256"}]}]


API = "https://api.kalos.fun"
SITE = "https://www.kalos.fun"
REWARDS = "0x14260bdc5a4d7c2e6bc35c65ae795d077499a875"  # ежедневная раздача 1M KALOS пропорционально поинтам
REWARDS_ABI = [
    {"type": "function", "name": "startRewardDay", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "uint64"}]},
    {"type": "function", "name": "allocations", "stateMutability": "view",
     "inputs": [{"name": "day", "type": "uint64"}, {"name": "who", "type": "address"}],
     "outputs": [{"name": "amountRaw", "type": "uint256"}, {"name": "claimed", "type": "bool"}]},
    {"type": "function", "name": "claimEpochs", "stateMutability": "nonpayable",
     "inputs": [{"name": "rewardDays", "type": "uint64[]"}], "outputs": []},
]


def _session(acc: dict):
    """Вход в Kalos по подписи (Sign-In with Ethereum): nonce → подпись → токен сессии. Через прокси аккаунта."""
    from curl_cffi import requests as http
    from eth_account import Account
    from eth_account.messages import encode_defunct
    s = http.Session(impersonate="chrome136", proxy=acc.get("proxy"), timeout=30)
    s.headers.update({"Origin": SITE, "Referer": SITE + "/rewards", "Accept": "application/json"})
    msg = s.post(API + "/v2/auth/nonce", json={"address": acc["address"]}).json()["message"]
    lines = msg.split("\n")
    if lines[1].lower() != acc["address"].lower() or "Chain ID: 99801" not in lines:  # подписываем только вход в Kalos
        raise RuntimeError("Kalos прислал неожиданное сообщение для подписи")
    sig = Account.sign_message(encode_defunct(text=msg), acc["key"]).signature.hex()
    r = s.post(API + "/v2/auth/verify", json={"message": msg, "signature": "0x" + sig.removeprefix("0x")}).json()
    if "token" not in r:
        raise RuntimeError(f"вход в Kalos не удался: {r}")
    s.headers["Authorization"] = "Bearer " + r["token"]
    return s


def checkin(acc: dict, cfg: Config, tag: str) -> None:
    """Ежедневный чек-ин на kalos.fun/rewards: доля в пуле поинтов дня + серия дней."""
    if not cfg.opt("kalos", "kalos_checkin", 0):
        return
    s = _session(acc)
    st = s.get(API + "/v2/points/me/checkin").json()
    if st.get("checkedIn"):
        log.info("%s: kalos: чек-ин сегодня уже был (серия %s дн.)", tag, st.get("streak"))
        return
    r = s.post(API + "/v2/points/checkin", json={})
    d = r.json()
    if r.status_code >= 300 or not d.get("checkedIn"):
        raise RuntimeError(f"чек-ин не прошёл: HTTP {r.status_code} {str(d)[:200]}")
    pool = d.get("pool") or {}
    log.info("%s: kalos: чек-ин, серия %s дн. (пул дня %s поинтов на %s чел.)", tag, d.get("streak"),
             pool.get("points"), pool.get("checkIns"))


def claim_rewards(acc: dict, cfg: Config, tag: str) -> None:
    """Забрать начисленный KALOS за прошлые дни (раздача по поинтам за сутки)."""
    if not cfg.opt("kalos", "kalos_claim", 0):
        return
    me = acc["address"]
    rw = contract(E, REWARDS, REWARDS_ABI, acc)
    today = int(time.time()) // 86400
    start = max(call(rw.functions.startRewardDay()), today - 30)
    days, total = [], 0
    for d in range(start, today):
        amount, claimed = call(rw.functions.allocations(d, me))
        if amount and not claimed:
            days.append(d)
            total += amount
    if not days:
        log.info("%s: kalos: награды KALOS к сбору нет", tag)
        return
    link = send_tx(E, acc, REWARDS, rw.encode_abi("claimEpochs", args=[days]))
    log.info("%s: kalos: забрал %s KALOS за %d дн., %s", tag, fmt(total), len(days), link)


def swap(acc: dict, cfg: Config, tag: str) -> None:
    mode = cfg.opt("kalos", "kalos_swap", 0)
    if not mode:
        return
    me = acc["address"]
    r = contract(E, ROUTER, ROUTER_ABI, acc)
    slip = cfg.opt("kalos", "slippage", 5.0) / 100
    amt = rand_amount(cfg.opt("kalos", "swap_min", 0.0005), cfg.opt("kalos", "swap_max", 0.001))
    deadline = int(time.time()) + 600

    buy = (cs(WHYPE), cs(KALOS), FEE, me, deadline, amt, 0, 0)
    out = simulate(r.functions.exactInputSingle(buy), acc, amt)
    buy = buy[:6] + (int(out * (1 - slip)), 0)
    before = erc20_balance(E, KALOS, me, acc)
    link = send_tx(E, acc, ROUTER, r.encode_abi("exactInputSingle", args=[buy]), value=amt)
    got = erc20_balance(E, KALOS, me, acc) - before
    log.info("%s: kalos: %s HYPE → %s KALOS, %s", tag, fmt(amt), fmt(got), link)
    if mode != 2 or got <= 0:
        return

    pause(cfg)
    ensure_allowance(E, acc, KALOS, ROUTER, got, tag, "kalos")
    # KALOS → WHYPE на сам роутер, затем unwrapWETH9 отдаёт HYPE на кошелёк — всё одной транзакцией
    sell = (cs(KALOS), cs(WHYPE), FEE, cs(ROUTER), int(time.time()) + 600, got, 0, 0)
    out = simulate(r.functions.exactInputSingle(sell), acc)
    min_out = int(out * (1 - slip))
    sell = sell[:6] + (min_out, 0)
    data = r.encode_abi("multicall", args=[[r.encode_abi("exactInputSingle", args=[sell]),
                                            r.encode_abi("unwrapWETH9", args=[min_out, me])]])
    link = send_tx(E, acc, ROUTER, data)
    log.info("%s: kalos: %s KALOS → %s HYPE, %s", tag, fmt(got), fmt(out), link)


def nft(acc: dict, cfg: Config, tag: str) -> None:
    if not cfg.opt("kalos", "kalos_nft", 0):
        return
    me = acc["address"]
    max_price = int(cfg.opt("kalos", "nft_max_price", 0.001) * 10**18)
    sd = contract(E, SEADROP, SEADROP_ABI, acc)
    seen, now, minted = set(), int(time.time()), 0
    calls = recent_calls(SEADROP, "mintPublic", pages=3)
    random.shuffle(calls)
    for c in calls:
        nft_addr, fee_rcpt = cs(c["params"]["nftContract"]), cs(c["params"]["feeRecipient"])
        if nft_addr in seen:
            continue
        seen.add(nft_addr)
        price, start, end, per_wallet, _, _ = call(sd.functions.getPublicDrop(nft_addr))
        if not (start <= now < end) or price > max_price:
            continue
        if per_wallet == 0 or call(contract(E, nft_addr, NFT_ABI, acc).functions.balanceOf(me)) > 0:
            continue  # из каждой коллекции — по одной NFT
        fn = sd.functions.mintPublic(nft_addr, fee_rcpt, "0x" + "0" * 40, 1)
        try:
            simulate(fn, acc, price)
        except Exception as e:
            log.debug("%s: kalos nft: дроп %s не минтится: %s", tag, nft_addr, e)
            continue
        link = send_tx(E, acc, SEADROP, sd.encode_abi("mintPublic", args=[nft_addr, fee_rcpt, "0x" + "0" * 40, 1]),
                       value=price)
        log.info("%s: kalos: минт NFT %s за %s HYPE, %s", tag, nft_addr, fmt(price), link)
        minted += 1
        if minted >= cfg.opt("kalos", "nft_per_day", 2):
            return
        pause(cfg)
    if minted:
        return
    log.info("%s: kalos: нет открытого публичного дропа дешевле %s HYPE (проверено %d коллекций)", tag,
             fmt(max_price), len(seen))


# ── маркетплейс Kalos: покупка NFT у других и выставление на продажу ─────────
# Поинты Kalos идут за покупки и продажи NFT (и платные минты), свопы токенов не считаются.
MARKETPLACE = "0x0FF8b7afC288AC42f82320f54a49c400f233bF07"
_BUY = {"name": "p", "type": "tuple[]", "components": [
    {"name": "nftAddress", "type": "address"}, {"name": "tokenId", "type": "uint256"},
    {"name": "owner", "type": "address"}, {"name": "quantity", "type": "uint64"},
    {"name": "maxPricePerItem", "type": "uint128"}, {"name": "paymentToken", "type": "address"},
    {"name": "usingNative", "type": "bool"}]}
_LIST = {"name": "p", "type": "tuple[]", "components": [
    {"name": "nftAddress", "type": "address"}, {"name": "tokenId", "type": "uint256"},
    {"name": "quantity", "type": "uint64"}, {"name": "pricePerItem", "type": "uint128"},
    {"name": "expirationTime", "type": "uint64"}, {"name": "paymentToken", "type": "address"}]}
MARKET_ABI = [
    {"type": "function", "name": "buyItems", "stateMutability": "payable", "inputs": [_BUY], "outputs": []},
    {"type": "function", "name": "createOrUpdateListings", "stateMutability": "nonpayable", "inputs": [_LIST],
     "outputs": []},
]
NFT721_ABI = [
    {"type": "function", "name": "ownerOf", "stateMutability": "view", "inputs": [{"name": "id", "type": "uint256"}],
     "outputs": [{"type": "address"}]},
    {"type": "function", "name": "isApprovedForAll", "stateMutability": "view",
     "inputs": [{"name": "o", "type": "address"}, {"name": "op", "type": "address"}], "outputs": [{"type": "bool"}]},
    {"type": "function", "name": "setApprovalForAll", "stateMutability": "nonpayable",
     "inputs": [{"name": "op", "type": "address"}, {"name": "ok", "type": "bool"}], "outputs": []},
]


def market(acc: dict, cfg: Config, tag: str) -> None:
    """Купить самый дешёвый ERC-721 лот у чужого продавца и сразу выставить его с наценкой."""
    from curl_cffi import requests as http
    from common import load_accounts
    if not cfg.opt("kalos", "kalos_market", 0):
        return
    me = acc["address"]
    max_price = int(cfg.opt("kalos", "market_max_price", 0.03) * 10**18)
    markup = cfg.opt("kalos", "market_markup", 5.0) / 100
    st = http.get(API + "/v2/market/state", impersonate="chrome136", timeout=60).json()
    standard = {c["slug"]: c.get("standard") for c in st.get("collections", [])}
    ours = {a["address"].lower() for a in load_accounts()}  # у своих аккаунтов не покупаем (wash trading)
    now = int(time.time())
    lots = [lt for lt in st.get("listings", [])
            if int(lt["expiration"]) > now + 600 and int(lt["price"]) <= max_price
            and standard.get(lt["slug"]) == "ERC721" and lt["seller"].lower() not in ours]
    lots.sort(key=lambda lt: int(lt["price"]))
    mk = contract(E, MARKETPLACE, MARKET_ABI, acc)
    for lot in lots[:15]:
        price, nft, token = int(lot["price"]), cs(lot["nft"]), int(lot["tokenId"])
        params = [(nft, token, cs(lot["seller"]), 1, price, cs(lot["paymentToken"]), True)]
        try:
            simulate(mk.functions.buyItems(params), acc, price)
        except Exception as e:
            log.debug("%s: kalos market: лот %s #%s недоступен: %s", tag, lot["slug"], token, e)
            continue
        link = send_tx(E, acc, MARKETPLACE, mk.encode_abi("buyItems", args=[params]), value=price)
        log.info("%s: kalos: куплен NFT %s #%s за %s HYPE, %s", tag, lot["slug"], token, fmt(price), link)
        pause(cfg)
        c = contract(E, nft, NFT721_ABI, acc)
        if call(c.functions.ownerOf(token)).lower() != me.lower():
            log.warning("%s: kalos: NFT %s #%s после покупки не на кошельке — не выставляю", tag, lot["slug"], token)
            return
        if not call(c.functions.isApprovedForAll(me, cs(MARKETPLACE))):
            send_tx(E, acc, nft, c.encode_abi("setApprovalForAll", args=[cs(MARKETPLACE), True]))
            pause(cfg)
        ask = int(price * (1 + markup))
        lst = [(nft, token, 1, ask, now + 30 * 86400, cs(lot["paymentToken"]))]
        link = send_tx(E, acc, MARKETPLACE, mk.encode_abi("createOrUpdateListings", args=[lst]))
        log.info("%s: kalos: NFT %s #%s выставлен за %s HYPE на 30 дней, %s", tag, lot["slug"], token, fmt(ask), link)
        return
    log.info("%s: kalos: нет подходящего лота дешевле %s HYPE", tag, fmt(max_price))
