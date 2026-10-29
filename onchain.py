"""Общее для действий в сети: вызовы контрактов, отправка транзакций, ERC-20, случайные суммы.

RPC, лимиты Conduit и прокси берутся из bridge.py (w3, with_retry), чтобы всё работало одинаково.
"""
from __future__ import annotations

import random
import threading
import time

import requests
from web3 import Web3

from bridge import (ELYSIUM_CHAIN_ID, ELYSIUM_RPC, ELYSIUM_TX, HYPEREVM_CHAIN_ID, HYPEREVM_RPC, HYPEREVM_TX,
                    MIN_GAS, NoFunds, fmt, w3, with_retry)
from common import Config

__all__ = ["E", "H", "NoFunds", "fmt", "cs", "contract", "send_tx", "erc20_balance", "ensure_allowance",
           "rand_amount", "pause", "fmt_units"]

# (rpc, chain_id, ссылка на транзакцию в эксплорере)
E = (ELYSIUM_RPC, ELYSIUM_CHAIN_ID, ELYSIUM_TX)
H = (HYPEREVM_RPC, HYPEREVM_CHAIN_ID, HYPEREVM_TX)

MAX_UINT = 2**256 - 1

ERC20_ABI = [
    {"type": "function", "name": "balanceOf", "stateMutability": "view",
     "inputs": [{"name": "a", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "allowance", "stateMutability": "view",
     "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "approve", "stateMutability": "nonpayable",
     "inputs": [{"name": "s", "type": "address"}, {"name": "v", "type": "uint256"}], "outputs": [{"type": "bool"}]},
]


def cs(addr: str) -> str:
    return Web3.to_checksum_address(addr)


def contract(net: tuple, addr: str, abi: list, acc: dict | None = None):
    return w3(net[0], acc).eth.contract(address=cs(addr), abi=abi)


def call(fn):
    """Чтение из контракта с повтором при лимите RPC: call(c.functions.balanceOf(a))."""
    return with_retry(fn.call)


def send_tx(net: tuple, acc: dict, to: str, data: str = "0x", value: int = 0, gas_mult: float = 1.6) -> str:
    """Подписать, отправить и дождаться транзакции. Каждый запрос к RPC — с повтором при лимите.
    Возвращает ссылку на транзакцию в эксплорере. Отказ в сети — RuntimeError, мало HYPE — NoFunds."""
    rpc, chain_id, explorer = net
    c = w3(rpc, acc)
    addr = acc["address"]
    tx = {"from": addr, "to": cs(to), "value": value, "data": data, "chainId": chain_id}
    have = with_retry(c.eth.get_balance, addr)
    if have <= value:
        raise NoFunds(f"нужно больше {fmt(value)} HYPE, есть {fmt(have)}" if value else f"баланс {fmt(have)} HYPE")
    try:
        # оценка Arbitrum занижает первую запись аккаунта: лимит с запасом (газ тут копеечный)
        tx["gas"] = max(int(with_retry(c.eth.estimate_gas, tx) * gas_mult), MIN_GAS)
    except Exception as e:
        if "insufficient funds" in str(e).lower():
            raise NoFunds(f"не хватает на газ, есть {fmt(have)} HYPE") from e
        raise
    base = with_retry(c.eth.get_block, "latest").get("baseFeePerGas")
    if base is not None:
        tip = with_retry(lambda: c.eth.max_priority_fee)
        tx["maxPriorityFeePerGas"] = tip
        tx["maxFeePerGas"] = max_price = base * 2 + tip
    else:
        tx["gasPrice"] = max_price = with_retry(lambda: c.eth.gas_price)
    need = value + tx["gas"] * max_price
    if have < need:
        raise NoFunds(f"нужно {fmt(need)} HYPE (с газом), есть {fmt(have)}")
    tx["nonce"] = with_retry(c.eth.get_transaction_count, addr, "pending")
    raw = c.eth.account.sign_transaction(tx, acc["key"]).raw_transaction
    h = with_retry(c.eth.send_raw_transaction, raw)
    r = with_retry(c.eth.wait_for_transaction_receipt, h, timeout=180, poll_latency=1)
    if r.status != 1:
        raise RuntimeError(f"транзакция отклонена в сети: {explorer}{h.to_0x_hex()}")
    return explorer + h.to_0x_hex()


def erc20_balance(net: tuple, token: str, who: str, acc: dict | None = None) -> int:
    return call(contract(net, token, ERC20_ABI, acc).functions.balanceOf(cs(who)))


def ensure_allowance(net: tuple, acc: dict, token: str, spender: str, amount: int, tag: str, what: str) -> None:
    """Если разрешение меньше amount — одобрить spender на максимум (один раз и навсегда)."""
    from common import log
    t = contract(net, token, ERC20_ABI, acc)
    if call(t.functions.allowance(acc["address"], cs(spender))) >= amount:
        return
    link = send_tx(net, acc, token, t.encode_abi("approve", args=[cs(spender), MAX_UINT]))
    log.info("%s: %s: approve, %s", tag, what, link)


def rand_amount(lo: float, hi: float, decimals: int = 18, digits: int = 6) -> int:
    """Случайная сумма от lo до hi в базовых единицах токена (округление до digits знаков)."""
    v = round(random.uniform(lo, hi), digits)
    return max(int(v * 10**decimals), int(lo * 10**decimals), 1)


def fmt_units(v: int, decimals: int) -> str:
    return f"{v / 10**decimals:.6f}".rstrip("0").rstrip(".") or "0"


_CALLS_CACHE: dict = {}
_CALLS_LOCK = threading.Lock()
CALLS_TTL = 600  # с, одна выборка на все потоки


def _plain(v):
    """Структуры из decode_function_input → кортежи (порядок полей как в ABI)."""
    if isinstance(v, dict):
        return tuple(_plain(x) for x in v.values())
    if isinstance(v, (list, tuple)):
        return type(v)(_plain(x) for x in v)
    return v


def _rpc_batch(batch: list) -> list:
    """Пакетный JSON-RPC к Elysium. HTTP 429 и ошибки внутри пакета — исключением, чтобы with_retry повторил."""
    r = requests.post(E[0], json=batch, timeout=60)
    r.raise_for_status()
    resp = r.json()
    if not isinstance(resp, list):
        raise RuntimeError(str(resp)[:300])
    errs = [x["error"] for x in resp if x.get("error")]
    if errs:
        raise RuntimeError(str(errs[0])[:300])
    return resp


def recent_calls(addr: str, method: str, abi: list, log_address: str | None = None,
                 topics: list | None = None, blocks: int = 20000, limit: int = 80) -> list[dict]:
    """Свежие успешные вызовы method у контракта addr — напрямую через RPC Elysium, без эксплорера.
    Транзакции находятся по событиям: логи log_address (по умолчанию сам addr) за последние blocks блоков
    (~1 ч при 20000), затем пакетом читаются сами транзакции и раскодируется их вызов по abi.
    Возвращает [{"params": {имя: значение}, "value": int, "from": адрес}], свежие первыми.
    Результат кэшируется на CALLS_TTL секунд: все аккаунты делят одну выборку."""
    key = (addr.lower(), method, (log_address or "").lower(), str(topics), blocks)
    with _CALLS_LOCK:
        hit = _CALLS_CACHE.get(key)
        if hit and time.time() - hit[0] < CALLS_TTL:
            return list(hit[1])
        c = w3(E[0])
        head = with_retry(lambda: c.eth.block_number)
        flt = {"fromBlock": max(head - blocks, 0), "toBlock": head, "address": cs(log_address or addr)}
        if topics:
            flt["topics"] = topics
        logs = with_retry(c.eth.get_logs, flt)
        hashes = list(dict.fromkeys(lg["transactionHash"].to_0x_hex() for lg in reversed(logs)))[:limit]
        dec = c.eth.contract(abi=abi)
        out = []
        for i in range(0, len(hashes), 10):  # Conduit считает каждый запрос пакета отдельно — пакеты малые
            if i:
                time.sleep(0.5)
            batch = [{"jsonrpc": "2.0", "id": j, "method": "eth_getTransactionByHash", "params": [h]}
                     for j, h in enumerate(hashes[i:i + 10])]
            for r in sorted(with_retry(_rpc_batch, batch), key=lambda x: x.get("id", 0)):
                tx = r.get("result") or {}
                if (tx.get("to") or "").lower() != addr.lower():
                    continue
                try:
                    fn, args = dec.decode_function_input(tx["input"])
                except Exception:
                    continue
                if fn.fn_name != method:
                    continue
                out.append({"params": {k: _plain(v) for k, v in args.items()},
                            "value": int(tx.get("value") or "0x0", 16), "from": tx["from"]})
        _CALLS_CACHE[key] = (time.time(), out)
        return list(out)


def topic_addr(a: str) -> str:
    """Адрес как индексированный topic события (32 байта)."""
    return "0x" + "0" * 24 + a.lower().removeprefix("0x")


def simulate(fn, acc: dict, value: int = 0):
    """eth_call от имени аккаунта: результат функции или исключение с причиной отката."""
    return with_retry(fn.call, {"from": acc["address"], "value": value})


def pause(cfg: Config) -> None:
    """Случайная пауза между действиями ([general] pause_min/pause_max, секунды)."""
    lo, hi = cfg.opt("general", "pause_min", 3.0), cfg.opt("general", "pause_max", 10.0)
    if hi > 0:
        time.sleep(random.uniform(min(lo, hi), hi))
