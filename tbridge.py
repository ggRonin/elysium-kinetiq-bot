"""Бридж ERC-20 (canonical) между Elysium и HyperEVM: USDC или WHYPE.

Elysium → HyperEVM: outboundTransfer на роутере Elysium (токен сжигается, без approve и без value).
    Токены станут доступны на HyperEVM после подтверждения rollup (~45–60 мин) и клейма в Outbox —
    клейм делает тот же код, что и для HYPE (bridge.claim_confirmed по pending-actions сайта).
HyperEVM → Elysium: approve gateway + outboundTransfer на роутере HyperEVM с оплатой доставки в HYPE.
    Параметры доставки считаются как на сайте: submission = 4× комиссии Inbox,
    maxGas = 3× оценки NodeInterface, ставка газа = 6× текущей цены газа Elysium.

[token_bridge] tbridge = 0 — пропуск, 1 — Elysium → HyperEVM, 2 — ещё и обратно (из баланса токена на HyperEVM;
для WHYPE недостающее заворачивается из HYPE на HyperEVM).
"""
from __future__ import annotations

from eth_abi import encode

import bridge
import swap
from common import Config, log
from onchain import (E, H, NoFunds, call, contract, cs, ensure_allowance, erc20_balance, fmt, fmt_units, pause,
                     rand_amount, send_tx)
from bridge import w3, with_retry

ROUTER_E = "0x89659883a9d980925733B0A698F117AAb65ac718"
ROUTER_H = "0x1aAE2caD8B0249905492087EF230FcCEa3707C45"
INBOX = "0x11178Df40CBf87a4343f4B3F8B09B366ceFA8Ec0"
NODE_INTERFACE = "0x00000000000000000000000000000000000000C8"

# token: (адрес на Elysium, адрес на HyperEVM, decimals)
TOKENS = {
    "USDC": ("0x7D29d8047B905000459c0E80c34a26CeedcB47b2", "0x2B3370eE501B4a559b57D449569354196457D8Ab", 6),
    "WHYPE": ("0xCD57F65c2B0e5881CFC2E609F7CD53b746E1F234", "0x5555555555555555555555555555555555555555", 18),
}
FALLBACK_MAX_GAS = 300_000

ROUTER_ABI = [
    {"type": "function", "name": "outboundTransfer", "stateMutability": "payable",
     "inputs": [{"name": "token", "type": "address"}, {"name": "to", "type": "address"},
                {"name": "amount", "type": "uint256"}, {"name": "maxGas", "type": "uint256"},
                {"name": "gasPriceBid", "type": "uint256"}, {"name": "data", "type": "bytes"}],
     "outputs": [{"type": "bytes"}]},
    {"type": "function", "name": "getGateway", "stateMutability": "view",
     "inputs": [{"name": "token", "type": "address"}], "outputs": [{"type": "address"}]},
]
GATEWAY_ABI = [
    {"type": "function", "name": "getOutboundCalldata", "stateMutability": "view",
     "inputs": [{"name": "token", "type": "address"}, {"name": "from", "type": "address"},
                {"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"},
                {"name": "data", "type": "bytes"}], "outputs": [{"type": "bytes"}]},
    {"type": "function", "name": "counterpartGateway", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "address"}]},
]
INBOX_ABI = [
    {"type": "function", "name": "calculateRetryableSubmissionFee", "stateMutability": "view",
     "inputs": [{"name": "dataLength", "type": "uint256"}, {"name": "baseFee", "type": "uint256"}],
     "outputs": [{"type": "uint256"}]},
]
NODE_ABI = [
    {"type": "function", "name": "estimateRetryableTicket", "stateMutability": "nonpayable",
     "inputs": [{"name": "sender", "type": "address"}, {"name": "deposit", "type": "uint256"},
                {"name": "to", "type": "address"}, {"name": "l2CallValue", "type": "uint256"},
                {"name": "excessFeeRefundAddress", "type": "address"},
                {"name": "callValueRefundAddress", "type": "address"}, {"name": "data", "type": "bytes"}],
     "outputs": []},
]
WHYPE_H_ABI = [{"type": "function", "name": "deposit", "stateMutability": "payable", "inputs": [], "outputs": []}]


def withdraw(acc: dict, sym: str, amount: int) -> str:
    """Elysium → HyperEVM: сжечь токен на Elysium. token — адрес на HyperEVM."""
    _, l1, _ = TOKENS[sym]
    r = contract(E, ROUTER_E, ROUTER_ABI, acc)
    return send_tx(E, acc, ROUTER_E, r.encode_abi("outboundTransfer", args=[cs(l1), acc["address"], amount, 0, 0, b""]))


def deposit(acc: dict, sym: str, amount: int, tag: str) -> str:
    """HyperEVM → Elysium: approve gateway, затем outboundTransfer с оплатой доставки."""
    _, l1, _ = TOKENS[sym]
    me = acc["address"]
    r = contract(H, ROUTER_H, ROUTER_ABI, acc)
    gw_addr = call(r.functions.getGateway(cs(l1)))
    gw = contract(H, gw_addr, GATEWAY_ABI, acc)
    ensure_allowance(H, acc, l1, gw_addr, amount, tag, f"бридж {sym}")

    calldata = call(gw.functions.getOutboundCalldata(cs(l1), me, me, amount, b""))
    base = with_retry(w3(H[0], acc).eth.get_block, "latest").get("baseFeePerGas") or with_retry(
        lambda: w3(H[0], acc).eth.gas_price)
    sub_fee = call(contract(H, INBOX, INBOX_ABI, acc).functions.calculateRetryableSubmissionFee(len(calldata), base))
    max_submission = 4 * sub_fee
    try:
        counterpart = call(gw.functions.counterpartGateway())
        node = contract(E, NODE_INTERFACE, NODE_ABI, acc)
        # WHYPE идёт через WETH-gateway: он разворачивает токен и шлёт сумму на Elysium как value
        call_value = amount if sym == "WHYPE" else 0
        est = with_retry(node.functions.estimateRetryableTicket(
            cs(gw_addr), 10**18 + call_value, counterpart, call_value, me, me, calldata).estimate_gas)
        max_gas = 3 * est
    except Exception as e:
        log.debug("%s: оценка доставки не удалась (%s), беру %d газа", tag, e, FALLBACK_MAX_GAS)
        max_gas = FALLBACK_MAX_GAS
    bid = 6 * with_retry(lambda: w3(E[0], acc).eth.gas_price)
    value = max_submission + max_gas * bid
    data = encode(["uint256", "bytes"], [max_submission, b""])
    return send_tx(H, acc, ROUTER_H,
                   r.encode_abi("outboundTransfer", args=[cs(l1), me, amount, max_gas, bid, data]), value=value)


def run(acc: dict, cfg: Config, tag: str) -> None:
    mode = cfg.opt("token_bridge", "tbridge", 0)
    if not mode:
        return
    sym = cfg.opt("token_bridge", "token", "USDC").upper()
    if sym not in TOKENS:
        log.error("%s: [token_bridge] token = %s — поддерживаются %s", tag, sym, ", ".join(TOKENS))
        return
    l2, l1, dec = TOKENS[sym]
    lo, hi = cfg.opt("token_bridge", "tbridge_min", 1.0), cfg.opt("token_bridge", "tbridge_max", 3.0)
    rounds = cfg.opt("token_bridge", "tbridge_rounds", 1)
    me = acc["address"]

    bridge.claim_confirmed(acc, tag, cfg.claim_min)  # заклеймить выводы, которые rollup уже подтвердил (HYPE и токены)

    for i in range(1, rounds + 1):
        amt = rand_amount(lo, hi, dec)
        have = erc20_balance(E, l2, me, acc)
        if have < amt and cfg.opt("token_bridge", "tbridge_buy", 1):
            # не хватает токена на Elysium — докупить: USDC через swap, WHYPE заворачиванием HYPE
            if sym == "USDC":
                swap.buy_usdc(acc, amt - have, tag)
            else:
                w = contract(E, l2, WHYPE_H_ABI, acc)
                link = send_tx(E, acc, l2, w.encode_abi("deposit"), value=amt - have)
                log.info("%s: бридж WHYPE: wrap %s HYPE на Elysium, %s", tag, fmt(amt - have), link)
            pause(cfg)
            have = erc20_balance(E, l2, me, acc)
        if have < amt:
            log.info("%s: бридж %s: на Elysium %s %s, нужно %s", tag, sym, fmt_units(have, dec), sym,
                     fmt_units(amt, dec))
            break
        link = withdraw(acc, sym, amt)
        log.info("%s: бридж %s %d/%d: %s %s Elysium → HyperEVM (клейм через ~1 ч), %s", tag, sym, i, rounds,
                 fmt_units(amt, dec), sym, link)
        if i < rounds:
            pause(cfg)

    if mode != 2:
        return
    pause(cfg)
    have = erc20_balance(H, l1, me, acc)
    if sym == "WHYPE":
        want = rand_amount(lo, hi, dec)
        if have < want:  # не хватает WHYPE на HyperEVM — завернуть недостающее из HYPE
            w = contract(H, l1, WHYPE_H_ABI, acc)
            try:
                link = send_tx(H, acc, l1, w.encode_abi("deposit"), value=want - have)
            except NoFunds as e:
                log.info("%s: бридж WHYPE: мало HYPE на HyperEVM для возврата: %s", tag, e)
                return
            log.info("%s: бридж WHYPE: wrap %s HYPE на HyperEVM, %s", tag, fmt(want - have), link)
            pause(cfg)
        amount = want
    else:
        amount = have  # весь USDC, вернувшийся на HyperEVM
    if amount <= 0:
        log.info("%s: бридж %s: на HyperEVM пока нет %s для возврата (ждёт клейма)", tag, sym, sym)
        return
    try:
        link = deposit(acc, sym, amount, tag)
    except NoFunds as e:
        log.info("%s: бридж %s: мало HYPE на HyperEVM для депозита: %s", tag, sym, e)
        return
    log.info("%s: бридж %s: %s %s HyperEVM → Elysium (~1 мин), %s", tag, sym, fmt_units(amount, dec), sym, link)
