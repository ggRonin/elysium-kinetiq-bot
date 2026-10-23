"""Бридж HYPE между Elysium testnet (L3, Arbitrum Orbit) и HyperEVM testnet.

Elysium → HyperEVM: ArbSys(0x64).withdrawEth(адрес) на Elysium. Деньги станут доступны на HyperEVM,
    когда rollup подтвердит вывод и его заклеймят (Outbox.executeTransaction на HyperEVM).
HyperEVM → Elysium: Inbox.depositEth() на HyperEVM, HYPE приходит на тот же адрес примерно за минуту.

За проход на каждом аккаунте делается bridge_rounds кругов подряд, ничего не дожидаясь
(транзакции отправляются пачкой с локальным nonce, квитанции проверяются в конце):
    bridge=1 — круг = вывод Elysium → HyperEVM;
    bridge=2 — круг = вывод Elysium → HyperEVM + депозит HyperEVM → Elysium (из баланса HyperEVM).
Перед кругами бот клеймит выводы, которые rollup уже подтвердил (если на HyperEVM есть газ).
RPC идёт напрямую (USE_RPC_PROXY), транзакции — по одной. Conduit пропускает ~2–3 транзакции в секунду
с одного IP на всех: на отказ «Rate Limit» аккаунт ждёт случайную паузу и повторяет.
"""
from __future__ import annotations

import random
import time

from curl_cffi import requests as http
from web3 import Web3

from common import Config, log, short

ELYSIUM_RPC = "https://testnet-rpc.elysium.kinetiq.xyz"
HYPEREVM_RPC = "https://rpc.hyperliquid-testnet.xyz/evm"
ELYSIUM_CHAIN_ID = 99801
HYPEREVM_CHAIN_ID = 998

ARB_SYS = "0x0000000000000000000000000000000000000064"
INBOX = "0x11178Df40CBf87a4343f4B3F8B09B366ceFA8Ec0"
OUTBOX = "0x87391D602aaffB3Fe8051EcCDb75d6EEe8C31060"
PENDING_API = "https://elysium.kinetiq.xyz/api/bridge/testnet/pending-actions"

ELYSIUM_TX = "https://elysium.kinetiq.xyz/testnet-explorer/transaction/"
HYPEREVM_TX = "https://testnet.hyperevm-explorer.xyz/tx/"

ABI = [
    {"type": "function", "name": "withdrawEth", "stateMutability": "payable",
     "inputs": [{"name": "destination", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "depositEth", "stateMutability": "payable",
     "inputs": [], "outputs": [{"type": "uint256"}]},
]
MIN_GAS = 150_000
MAX_ERRORS = 3      # ошибок отправки подряд — и серия транзакций останавливается

_w3: dict[tuple, Web3] = {}


USE_RPC_PROXY = False  # True — RPC через прокси аккаунта (свой IP = свой лимит Conduit, быстрее, но тратит трафик)


def proxy_of(acc: dict | None) -> str | None:
    """Прокси для RPC; None — напрямую."""
    if not USE_RPC_PROXY or acc is None or acc.get("rpc_direct"):
        return None
    return acc.get("proxy")


def w3(rpc: str, acc: dict | None = None) -> Web3:
    px = proxy_of(acc)
    key = (rpc, px, acc["address"] if acc else None)  # своё соединение у каждого аккаунта
    if key not in _w3:
        kw = {"timeout": 30}
        if px:
            kw["proxies"] = {"http": px, "https": px}
        _w3[key] = Web3(Web3.HTTPProvider(rpc, request_kwargs=kw))
    return _w3[key]


def rate_limited(e: Exception) -> bool:
    """Лимит Conduit: приходит и как JSON-ошибка -32017, и как HTTP 429."""
    t = str(e).lower()
    return "rate limit" in t or "-32017" in t or "429" in t or "too many requests" in t


def with_retry(fn, *args, tries: int = 40, **kw):
    """Вызов RPC с повтором при лимите Conduit: случайная пауза 1–5 с (до ~2 мин всего)."""
    for i in range(tries):
        try:
            return fn(*args, **kw)
        except Exception as e:
            if not rate_limited(e) or i == tries - 1:
                raise
            time.sleep(random.uniform(1, 5))


def fmt(wei: int) -> str:
    return f"{wei / 10**18:.6f}".rstrip("0").rstrip(".")


class NoFunds(Exception):
    """Не хватает HYPE на сумму и/или газ."""


def send(rpc: str, chain_id: int, acc: dict, to: str, value: int = 0, data: str = "0x") -> str:
    """Подписать и отправить транзакцию, дождаться квитанции. Возвращает хэш."""
    c = w3(rpc, acc)
    addr = acc["address"]
    tx = {"from": addr, "to": Web3.to_checksum_address(to), "value": value, "data": data,
          "chainId": chain_id, "nonce": c.eth.get_transaction_count(addr, "pending")}
    have = c.eth.get_balance(addr)
    if have <= value:
        raise NoFunds(f"нужно больше {fmt(value)} HYPE, есть {fmt(have)}" if value else f"баланс {fmt(have)} HYPE")
    try:
        # оценка Arbitrum занижает первую запись аккаунта: лимит с большим запасом (газ тут копеечный)
        tx["gas"] = max(int(c.eth.estimate_gas(tx) * 1.6), MIN_GAS)
    except Exception as e:
        if "insufficient funds" in str(e):
            raise NoFunds(f"не хватает на газ, есть {fmt(have)} HYPE") from e
        raise
    base = c.eth.get_block("latest").get("baseFeePerGas")
    if base is not None:
        tip = c.eth.max_priority_fee
        tx["maxPriorityFeePerGas"] = tip
        tx["maxFeePerGas"] = base * 2 + tip
        max_price = tx["maxFeePerGas"]
    else:
        tx["gasPrice"] = max_price = c.eth.gas_price
    need = value + tx["gas"] * max_price
    if have < need:
        raise NoFunds(f"нужно {fmt(need)} HYPE (с газом), есть {fmt(have)}")
    h = c.eth.send_raw_transaction(c.eth.account.sign_transaction(tx, acc["key"]).raw_transaction)
    r = c.eth.wait_for_transaction_receipt(h, timeout=180)
    if r.status != 1:
        raise RuntimeError(f"транзакция {h.to_0x_hex()} отклонена в сети")
    return h.to_0x_hex()


def _calldata(fn: str, *args) -> str:
    return w3(ELYSIUM_RPC).eth.contract(abi=ABI).encode_abi(fn, args=list(args))


def pending(addr: str, acc: dict | None = None) -> dict:
    """Выводы аккаунта с сайта Elysium. Сайт под нагрузкой отвечает 502 / не отвечает — повтор с паузой."""
    for attempt in range(4):
        try:
            r = http.get(PENDING_API, params={"address": addr}, impersonate="chrome136", timeout=30,
                         proxy=proxy_of(acc))
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == 3:
                raise
            time.sleep(random.uniform(5, 15) * (attempt + 1))


def claim_confirmed(acc: dict, tag: str, min_hype: float = 0.0) -> None:
    """Заклеймить на HyperEVM выводы, которые rollup уже подтвердил.
    Выводы HYPE меньше min_hype пропускаются: газ за клейм дороже самой суммы. Токены клеймятся всегда."""
    try:
        p = pending(acc["address"], acc)
    except Exception as e:
        log.warning("%s: бридж: pending-actions недоступен (%s)", tag, e)
        return
    if p.get("retries"):
        log.warning("%s: бридж: %d депозит(ов) не доставлены на Elysium, их надо повторить на сайте (Retry)",
                    tag, len(p["retries"]))
    dust = int(min_hype * 10**18)
    is_dust = lambda w: w.get("symbol", "HYPE") == "HYPE" and int(w["amount"]) < dust
    ready = [w for w in p.get("withdrawals", []) if w.get("claim") and not is_dust(w)]
    ready.sort(key=lambda w: (w.get("symbol", "HYPE") != "HYPE", -int(w["amount"])))  # сначала крупный HYPE: это газ
    waiting = len([w for w in p.get("withdrawals", []) if not w.get("claim")])
    if waiting:
        log.info("%s: бридж: %d%s вывод(ов) ждут подтверждения rollup (~45–60 мин)", tag, waiting,
                 "+" if len(p.get("withdrawals", [])) >= 50 else "")
    for wd in ready:
        claim = wd["claim"]
        what = f"{int(wd['amount']) / 10 ** int(wd.get('decimals', 18)):.6f}".rstrip("0").rstrip(".") + " " + wd.get("symbol", "HYPE")
        if Web3.to_checksum_address(claim.get("outbox", "0x" + "0" * 40)) != OUTBOX:
            log.warning("%s: бридж: клейм %s ведёт на незнакомый Outbox, пропускаю", tag, wd.get("id"))
            continue
        try:
            for attempt in range(6):  # публичный RPC HyperEVM отвечает «rate limited» — подождать и повторить
                try:
                    h = send(HYPEREVM_RPC, HYPEREVM_CHAIN_ID, acc, OUTBOX, data=claim["calldata"])
                    break
                except NoFunds:
                    raise
                except Exception as e:
                    if not rate_limited(e) or attempt == 5:
                        raise
                    time.sleep(random.uniform(3, 8))
        except NoFunds as e:
            log.warning("%s: бридж: %d вывод(ов) готовы к клейму, но на HyperEVM нет HYPE на газ (%s)", tag, len(ready), e)
            return
        except Exception as e:
            log.warning("%s: бридж: клейм %s не прошёл: %s", tag, what, e)
            continue
        log.info("%s: бридж: клейм %s на HyperEVM, %s%s", tag, what, HYPEREVM_TX, h)


def burst(rpc: str, chain_id: int, acc: dict, to: str, data: str, amounts: list[int], window: int,
          what: str, explorer: str, start: int = 0, total: int | None = None, reserve: int = 0) -> list[str]:
    """Отправить подряд транзакции на `to` с суммами amounts, не дожидаясь каждой.

    Nonce, лимит газа и цена считаются один раз. Сумм берётся столько, сколько покрывает баланс.
    window — сколько транзакций отправлять до ожидания квитанции (мемпул HyperEVM держит мало).
    Возвращает хэши транзакций, прошедших в сети (status=1).
    """
    c = w3(rpc, acc)
    addr = acc["address"]
    base_tx = {"from": addr, "to": Web3.to_checksum_address(to), "data": data, "chainId": chain_id}
    try:
        # оценка Arbitrum занижает первую запись аккаунта: лимит с большим запасом (газ тут копеечный)
        gas = max(int(with_retry(c.eth.estimate_gas, {**base_tx, "value": min(amounts)}) * 2), MIN_GAS)
    except Exception as e:
        if "insufficient funds" in str(e):
            raise NoFunds(f"есть {fmt(c.eth.get_balance(addr))} HYPE") from e
        raise
    base = with_retry(c.eth.get_block, "latest").get("baseFeePerGas")
    if base is not None:
        tip = with_retry(lambda: c.eth.max_priority_fee)
        price = {"maxFeePerGas": base * 3 + tip, "maxPriorityFeePerGas": tip}
        max_price = price["maxFeePerGas"]
    else:
        price = {"gasPrice": with_retry(lambda: c.eth.gas_price)}
        max_price = price["gasPrice"]

    # сколько кругов покрывает баланс (reserve не трогаем: это газ на будущие клеймы)
    have = with_retry(c.eth.get_balance, addr) - reserve
    fit, spent = [], 0
    for a in amounts:
        if spent + a + gas * max_price > have:
            break
        fit.append(a)
        spent += a + gas * max_price
    if not fit:
        raise NoFunds(f"нужно {fmt(amounts[0] + gas * max_price)} HYPE (с газом), есть {fmt(have)}")

    nonce = with_retry(c.eth.get_transaction_count, addr, "pending")
    sent: list[str] = []
    errors = 0
    for n, a in enumerate(fit, 1):
        tx = {**base_tx, **price, "value": a, "gas": gas, "nonce": nonce}
        try:
            h = with_retry(c.eth.send_raw_transaction, c.eth.account.sign_transaction(tx, acc["key"]).raw_transaction)
        except Exception as e:
            errors += 1
            log.debug("#%d %s: отправка nonce %d: %s", acc["n"], short(addr), nonce, e)
            if errors >= MAX_ERRORS:
                log.warning("#%d %s: отправка остановлена после %d ошибок: %s", acc["n"], short(addr), errors, e)
                break
            nonce = with_retry(c.eth.get_transaction_count, addr, "pending")  # nonce мог разъехаться
            continue
        errors = 0
        nonce += 1
        sent.append(h.to_0x_hex())
        log.info("#%d %s: бридж: круг %d/%d, %s %s HYPE отправлен, %s%s",
                 acc["n"], short(addr), start + len(sent), total or len(amounts), what, fmt(a), explorer, sent[-1])
        if n % window == 0 and n < len(fit):
            with_retry(c.eth.wait_for_transaction_receipt, h, timeout=180, poll_latency=1)
    if not sent:
        return []

    # транзакции одного адреса исполняются по порядку: дождались последней — значит, исполнены все
    with_retry(c.eth.wait_for_transaction_receipt, sent[-1], timeout=180, poll_latency=1)
    ok = []
    for i in range(0, len(sent), 100):
        chunk = sent[i:i + 100]
        batch = [{"jsonrpc": "2.0", "id": j, "method": "eth_getTransactionReceipt", "params": [h]}
                 for j, h in enumerate(chunk)]
        replies = with_retry(lambda: http.post(rpc, json=batch, timeout=60, proxy=proxy_of(acc)).json())
        if isinstance(replies, dict) or any("error" in r for r in replies):  # лимит на пакет: по одной
            replies = [{"id": j, "result": with_retry(c.eth.get_transaction_receipt, h)} for j, h in enumerate(chunk)]
        replies = sorted(replies, key=lambda r: r["id"])
        ok += [h for h, r in zip(chunk, replies) if (r.get("result") or {}).get("status") in ("0x1", 1)]
    return ok


def run(acc: dict, cfg: Config, rounds: int | None = None, start: int = 0) -> int:
    """Проход бриджа для аккаунта: клейм подтверждённых выводов, затем `rounds` кругов подряд
    (по умолчанию bridge_rounds; start — сколько уже сделано, для нумерации). Возвращает, сколько сделано."""
    addr = acc["address"]
    tag = f"#{acc['n']} {short(addr)}"
    if proxy_of(acc):
        try:
            w3(ELYSIUM_RPC, acc).eth.block_number
        except Exception as e:
            if not rate_limited(e):
                log.warning("%s: прокси не отвечает для RPC (%s), бридж пойдёт напрямую", tag, type(e).__name__)
                acc["rpc_direct"] = True
    claim_confirmed(acc, tag, cfg.claim_min)

    amounts = [max(Web3.to_wei(round(random.uniform(cfg.bridge_min, cfg.bridge_max), 6), "ether"),
                   Web3.to_wei(cfg.bridge_min, "ether")) for _ in range(rounds or cfg.bridge_rounds)]
    try:
        ok = burst(ELYSIUM_RPC, ELYSIUM_CHAIN_ID, acc, ARB_SYS, _calldata("withdrawEth", addr), amounts,
                   window=10**9, what="вывод Elysium → HyperEVM", explorer=ELYSIUM_TX,
                   start=start, total=cfg.bridge_rounds)
    except NoFunds as e:
        log.info("%s: бридж: мало HYPE на Elysium для вывода: %s", tag, e)
        return 0
    except Exception as e:
        log.warning("%s: бридж: выводы не прошли: %s", tag, e)
        return 0
    log.info("%s: бридж: серия — %d/%d вывод(ов) Elysium → HyperEVM подтверждены в сети", tag, len(ok), len(amounts))

    if cfg.bridge == 2 and ok:
        try:
            back = burst(HYPEREVM_RPC, HYPEREVM_CHAIN_ID, acc, INBOX, _calldata("depositEth"), amounts[:len(ok)],
                         window=8, what="депозит HyperEVM → Elysium", explorer=HYPEREVM_TX,
                         start=start, total=cfg.bridge_rounds,
                         reserve=Web3.to_wei(cfg.opt("bridge", "hyperevm_reserve", 0.001), "ether"))
            log.info("%s: бридж: итог — %d/%d депозит(ов) HyperEVM → Elysium подтверждены в сети", tag, len(back), len(ok))
        except NoFunds as e:
            log.warning("%s: бридж: депозит обратно невозможен, мало HYPE на HyperEVM: %s", tag, e)
        except Exception as e:
            log.warning("%s: бридж: депозиты не прошли: %s", tag, e)
    return len(ok)
