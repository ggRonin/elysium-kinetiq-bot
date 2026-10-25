"""Газ на HyperEVM testnet для всех аккаунтов.

Без HYPE на HyperEVM нельзя получить выводы из Elysium (клейм — транзакция на HyperEVM) и вернуть средства обратно.
Кран Alchemy даёт HYPE только адресам с 0.01 HYPE в мейннете Hyperliquid, поэтому схема такая:
  1. Донор (аккаунт с HYPE на HyperEVM) докидывает каждому, у кого меньше topup_below, до topup HYPE.
  2. Каждый аккаунт, у кого на HyperEVM меньше big_withdraw/2 и нет такого вывода в пути, один раз выводит
     big_withdraw HYPE из Elysium. Через ~1 ч rollup подтвердит, и main.py (бридж) заклеймит его на этот газ.

    python hyperevm_gas.py              # всё по config.ini, раздел [hyperevm_gas]
    python hyperevm_gas.py --dry        # только показать, что будет сделано
"""
from __future__ import annotations

import argparse
import logging
import time
import sys

from web3 import Web3

import bridge
from common import load_accounts, load_config, log, short
from onchain import E, H, NoFunds, fmt, send_tx


def main() -> None:
    ap = argparse.ArgumentParser(description="Газ на HyperEVM: раздача от донора + крупный вывод из Elysium")
    ap.add_argument("--dry", action="store_true", help="ничего не отправлять, только показать план")
    args = ap.parse_args()
    cfg = load_config()
    donor_n = cfg.opt("hyperevm_gas", "donor", 1)
    topup = Web3.to_wei(cfg.opt("hyperevm_gas", "topup", 0.002), "ether")
    below = Web3.to_wei(cfg.opt("hyperevm_gas", "topup_below", 0.001), "ether")
    big = Web3.to_wei(cfg.opt("hyperevm_gas", "big_withdraw", 0.05), "ether")

    accounts = load_accounts()
    donor = next((a for a in accounts if a["n"] == donor_n), None)
    if donor is None:
        sys.exit(f"[hyperevm_gas] donor = {donor_n}: нет такой строки в accounts.txt")
    h3 = bridge.w3(H[0])
    bal_h = {a["address"]: bridge.with_retry(h3.eth.get_balance, a["address"]) for a in accounts}
    log.info("донор #%d %s: на HyperEVM %s HYPE", donor["n"], short(donor["address"]), fmt(bal_h[donor["address"]]))

    # 1. раздача газа от донора
    try:
        _give_gas(accounts, donor, bal_h, topup, below, args.dry)
    except NoFunds as e:
        log.error("донор: HYPE на HyperEVM кончился (%s) — раздача остановлена", e)
    _big_withdrawals(accounts, bal_h, big, below, args.dry)


def _give_gas(accounts, donor, bal_h, topup, below, dry) -> None:
    for a in accounts:
        if a is donor or bal_h[a["address"]] >= below:
            continue
        need = topup - bal_h[a["address"]]
        tag = f"#{a['n']} {short(a['address'])}"
        if dry:
            log.info("%s: [dry] донор отправит %s HYPE на HyperEVM", tag, fmt(need))
            continue
        link = None
        for attempt in range(3):  # публичный RPC HyperEVM иногда отвечает «invalid block height» — повторить
            try:
                link = send_tx(H, donor, a["address"], value=need, gas_mult=1.2)
                break
            except NoFunds:
                raise
            except Exception as e:
                log.warning("%s: отправка газа, попытка %d: %s", tag, attempt + 1, str(e)[:150])
                time.sleep(5)
        if link is None:
            continue
        bal_h[a["address"]] += need
        log.info("%s: газ от донора %s HYPE на HyperEVM, %s", tag, fmt(need), link)


def _big_withdrawals(accounts, bal_h, big, below, dry) -> None:
    """Крупный вывод из Elysium тем, у кого на HyperEVM мало и такой вывод ещё не в пути."""
    e3 = bridge.w3(E[0])
    abi = [{"type": "function", "name": "withdrawEth", "stateMutability": "payable",
            "inputs": [{"name": "destination", "type": "address"}], "outputs": [{"type": "uint256"}]}]
    arbsys = e3.eth.contract(address=bridge.ARB_SYS, abi=abi)
    for a in accounts:
        tag = f"#{a['n']} {short(a['address'])}"
        if bal_h[a["address"]] >= below:  # газ на HyperEVM есть (сбор HYPE оставляет там hyperevm_keep)
            continue
        try:
            pend = bridge.pending(a["address"], a)["withdrawals"]
        except Exception as e:
            log.warning("%s: pending-actions недоступен (%s) — пропуск", tag, e)
            continue
        if any(w.get("symbol", "HYPE") == "HYPE" and int(w["amount"]) >= big // 2 for w in pend):
            log.info("%s: крупный вывод уже в пути — пропуск", tag)
            continue
        if dry:
            log.info("%s: [dry] вывод %s HYPE Elysium → HyperEVM", tag, fmt(big))
            continue
        try:
            link = send_tx(E, a, bridge.ARB_SYS, arbsys.encode_abi("withdrawEth", args=[a["address"]]), value=big)
        except NoFunds as e:
            log.info("%s: мало HYPE на Elysium для вывода: %s", tag, e)
            continue
        except Exception as e:
            log.warning("%s: вывод не прошёл: %s", tag, str(e)[:200])
            continue
        log.info("%s: вывод %s HYPE Elysium → HyperEVM (клейм через ~1 ч), %s", tag, fmt(big), link)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("web3", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    main()
