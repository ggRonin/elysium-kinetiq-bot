"""Тестовый HYPE из тестнета Hyperliquid → HyperEVM testnet, для каждого аккаунта.

Шаги (как вручную на app.hyperliquid-testnet.xyz):
  1. Кран /drip: POST /info {"type":"claimDrip","user": адрес} — 1000 тестовых USDC (без подписи).
     Кран выдаёт только адресам, у которых есть аккаунт в мейннете Hyperliquid (был депозит), раз в сутки.
  2. Покупка HYPE за USDC в споте (пара HYPE/USDC = @1035), рыночный IoC-ордер. Минимум ордера — $10.
  3. Перевод HYPE из спота в HyperEVM: sendAsset на системный адрес HYPE 0x2222…2222
     (старый spotSend при «unified account» запрещён).

Подпись действий Hyperliquid перенесена из официального SDK (hyperliquid-python-sdk, MIT), чтобы не тянуть
его зависимости: L1-действия (ордер) — msgpack-хэш + EIP-712 «phantom agent», sendAsset — обычный EIP-712.

    python hl_faucet.py              # все аккаунты
    python hl_faucet.py --limit 3    # только первые N
Также запускается раз в сутки из main.py, если в config.ini [hl_faucet] hl_faucet = 1.
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
import time
from decimal import Decimal

import msgpack
from curl_cffi import requests as http
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_hex
from web3 import Web3

from common import Config, load_accounts, load_config, log, mask_proxy, short

API = "https://api.hyperliquid-testnet.xyz"
HYPEREVM_RPC = "https://rpc.hyperliquid-testnet.xyz/evm"
HYPE_SYSTEM = "0x2222222222222222222222222222222222222222"  # перевод на этот адрес = HYPE в HyperEVM
PAIR = "@1035"            # спот HYPE/USDC в тестнете
MIN_ORDER_USD = 10.0      # меньше Hyperliquid не принимает

_EIP712_DOMAIN = [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                  {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}]
_SEND_ASSET_TYPES = [
    {"name": "hyperliquidChain", "type": "string"}, {"name": "destination", "type": "string"},
    {"name": "sourceDex", "type": "string"}, {"name": "destinationDex", "type": "string"},
    {"name": "token", "type": "string"}, {"name": "amount", "type": "string"},
    {"name": "fromSubAccount", "type": "string"}, {"name": "nonce", "type": "uint64"}]


# ── подпись (из hyperliquid-python-sdk) ──────────────────────────────────────
def _sign(key: bytes, data: dict) -> dict:
    signed = Account.sign_message(encode_typed_data(full_message=data), key)
    return {"r": to_hex(signed.r), "s": to_hex(signed.s), "v": signed.v}


def _sign_l1(key: bytes, action: dict, nonce: int) -> dict:
    """L1-действие (ордер): keccak(msgpack(action) + nonce + флаг без vault) подписывается как «Agent»."""
    h = keccak(msgpack.packb(action) + nonce.to_bytes(8, "big") + b"\x00")
    return _sign(key, {
        "domain": {"chainId": 1337, "name": "Exchange", "verifyingContract": "0x" + "0" * 40, "version": "1"},
        "types": {"Agent": [{"name": "source", "type": "string"}, {"name": "connectionId", "type": "bytes32"}],
                  "EIP712Domain": _EIP712_DOMAIN},
        "primaryType": "Agent",
        "message": {"source": "b", "connectionId": h},  # "b" — тестнет
    })


def _sign_user(key: bytes, action: dict, types: list, primary: str) -> dict:
    """Действие, подписанное пользователем (sendAsset и т.п.)."""
    action["signatureChainId"] = "0x66eee"
    action["hyperliquidChain"] = "Testnet"
    return _sign(key, {
        "domain": {"name": "HyperliquidSignTransaction", "version": "1", "chainId": 0x66eee,
                   "verifyingContract": "0x" + "0" * 40},
        "types": {primary: types, "EIP712Domain": _EIP712_DOMAIN},
        "primaryType": primary,
        "message": action,
    })


def _wire(x: float) -> str:
    return f"{Decimal(f'{x:.8f}').normalize():f}"


# ── API ──────────────────────────────────────────────────────────────────────
def _info(s, body: dict):
    return s.post(API + "/info", json=body).json()


def _exchange(s, action: dict, signature: dict, nonce: int):
    payload = {"action": action, "nonce": nonce, "signature": signature, "vaultAddress": None, "expiresAfter": None}
    return s.post(API + "/exchange", json=payload).json()


def _spot(s, addr: str) -> dict:
    return {b["coin"]: (float(b["total"]), float(b.get("hold", 0)))
            for b in _info(s, {"type": "spotClearinghouseState", "user": addr}).get("balances", [])}


def run(acc: dict, cfg: Config, tag: str) -> None:
    if not cfg.opt("hl_faucet", "hl_faucet", 0):
        return
    addr, key = acc["address"], acc["key"]
    def session(proxy):
        s = http.Session(impersonate="chrome136", proxy=proxy, timeout=30)
        s.headers.update({"Origin": "https://app.hyperliquid-testnet.xyz", "Content-Type": "application/json"})
        return s

    # 1. кран: пустой ответ — успех, строка — причина отказа. Прокси не отвечает — напрямую.
    s = session(acc.get("proxy"))
    try:
        r = _info(s, {"type": "claimDrip", "user": addr})
    except Exception as e:
        if not acc.get("proxy"):
            raise
        log.info("%s: hl: прокси не отвечает (%s), иду напрямую", tag, str(e)[:80])
        s = session(None)
        r = _info(s, {"type": "claimDrip", "user": addr})
    if r:
        if "does not exist on mainnet" in str(r):
            log.info("%s: hl: кран недоступен — у адреса нет аккаунта в мейннете Hyperliquid", tag)
        else:
            log.info("%s: hl: кран: %s", tag, r)
    else:
        log.info("%s: hl: кран — получено 1000 тестовых USDC", tag)
        time.sleep(2)

    # 2. купить HYPE на весь USDC (кроме keep_usdc)
    bal = _spot(s, addr)
    usdc = bal.get("USDC", (0, 0))
    usdc = usdc[0] - usdc[1] - cfg.opt("hl_faucet", "keep_usdc", 1.0)
    if usdc >= MIN_ORDER_USD:
        meta = _info(s, {"type": "spotMeta"})
        idx = next(u["index"] for u in meta["universe"] if u["name"] == PAIR)
        sz_dec = next(t["szDecimals"] for t in meta["tokens"] if t["name"] == "HYPE")
        ask = float(_info(s, {"type": "l2Book", "coin": PAIR})["levels"][1][0]["px"])
        slip = cfg.opt("hl_faucet", "slippage", 2.0) / 100
        px = round(float(f"{ask * (1 + slip):.5g}"), 8 - sz_dec)
        sz = math.floor(usdc / (ask * (1 + slip)) * 10**sz_dec) / 10**sz_dec
        if sz * ask >= MIN_ORDER_USD:
            nonce = int(time.time() * 1000)
            action = {"type": "order", "orders": [{"a": 10000 + idx, "b": True, "p": _wire(px), "s": _wire(sz),
                                                  "r": False, "t": {"limit": {"tif": "Ioc"}}}], "grouping": "na"}
            res = _exchange(s, action, _sign_l1(key, action, nonce), nonce)
            st = (res.get("response") or {}).get("data", {}).get("statuses", [{}])[0] if isinstance(res, dict) else {}
            if "filled" in st:
                log.info("%s: hl: куплено %s HYPE по %s USDC", tag, st["filled"]["totalSz"], st["filled"]["avgPx"])
            else:
                log.warning("%s: hl: покупка не прошла: %s", tag, str(res)[:300])
            time.sleep(1)

    # 3. весь HYPE из спота → HyperEVM
    hype = _spot(s, addr).get("HYPE", (0, 0))
    amount = round(hype[0] - hype[1], 8)
    if amount <= 0:
        return
    meta = _info(s, {"type": "spotMeta"})
    token_id = next(t["tokenId"] for t in meta["tokens"] if t["name"] == "HYPE")
    w3 = Web3(Web3.HTTPProvider(HYPEREVM_RPC, request_kwargs={"timeout": 30}))
    before = w3.eth.get_balance(addr)
    nonce = int(time.time() * 1000)
    action = {"type": "sendAsset", "destination": HYPE_SYSTEM, "sourceDex": "spot", "destinationDex": "spot",
              "token": f"HYPE:{token_id}", "amount": _wire(amount), "fromSubAccount": "", "nonce": nonce}
    sig = _sign_user(key, action, _SEND_ASSET_TYPES, "HyperliquidTransaction:SendAsset")
    res = _exchange(s, action, sig, nonce)
    if not (isinstance(res, dict) and res.get("status") == "ok"):
        log.warning("%s: hl: перевод в HyperEVM не прошёл: %s", tag, str(res)[:300])
        return
    for _ in range(20):
        time.sleep(3)
        now = w3.eth.get_balance(addr)
        if now > before:
            break
    log.info("%s: hl: %s HYPE → HyperEVM, баланс там %s HYPE", tag, _wire(amount), Web3.from_wei(now, "ether"))


def main() -> None:
    ap = argparse.ArgumentParser(description="Тестовый HYPE: кран Hyperliquid → покупка HYPE → HyperEVM")
    ap.add_argument("--limit", type=int, default=None, help="только первые N аккаунтов")
    args = ap.parse_args()
    cfg = load_config()
    cfg.ini.set("hl_faucet", "hl_faucet", "1") if cfg.ini.has_section("hl_faucet") else (
        cfg.ini.add_section("hl_faucet"), cfg.ini.set("hl_faucet", "hl_faucet", "1"))
    for acc in load_accounts()[: args.limit]:
        tag = f"#{acc['n']} {short(acc['address'])}"
        try:
            run(acc, cfg, tag)
        except Exception as e:
            log.warning("%s (%s): hl: ошибка: %s", tag, mask_proxy(acc.get("proxy")), str(e)[:300])


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("web3", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    main()
