"""Кран Alchemy для HyperEVM testnet: 0.1 HYPE на адрес раз в 24 ч (www.alchemy.com/faucets/hyperliquid-testnet).

Нужен, чтобы на HyperEVM был газ: без него не получить выводы из Elysium и не вернуть токены обратно.
Проход: Turnstile (CapSolver) → POST /api/faucets/hyperliquid-testnet/send {address, turnstileToken}
→ опрос /status до успеха. Запросы идут через прокси аккаунта (лимит у крана может быть и по IP).

    python alchemy_faucet.py            # все аккаунты, у которых на HyperEVM меньше min_balance
    python alchemy_faucet.py --limit 3  # только первые N
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from curl_cffi.requests import AsyncSession
from dotenv import load_dotenv
from web3 import Web3

from common import ROOT, load_accounts, load_config, log, mask_proxy, short

FAUCET_ID = "hyperliquid-testnet"
PAGE = "https://www.alchemy.com/faucets/hyperliquid-testnet"
API = f"https://www.alchemy.com/api/faucets/{FAUCET_ID}"
TURNSTILE_KEY = "0x4AAAAAAB_Xdru0nbY8rQu_"
HYPEREVM_RPC = "https://rpc.hyperliquid-testnet.xyz/evm"
EXPLORER = "https://testnet.hyperevm-explorer.xyz/tx/"
IMPERSONATE = "chrome136"


async def solve_turnstile(api_key: str) -> str:
    async with AsyncSession() as s:
        r = (await s.post("https://api.capsolver.com/createTask", json={
            "clientKey": api_key,
            "task": {"type": "AntiTurnstileTaskProxyLess", "websiteURL": PAGE, "websiteKey": TURNSTILE_KEY},
        }, timeout=30)).json()
        if r.get("errorId"):
            raise RuntimeError(f"capsolver: {r.get('errorCode')} {r.get('errorDescription', '')}")
        task_id = r["taskId"]
        for _ in range(40):
            await asyncio.sleep(3)
            r = (await s.post("https://api.capsolver.com/getTaskResult",
                              json={"clientKey": api_key, "taskId": task_id}, timeout=30)).json()
            if r.get("errorId"):
                raise RuntimeError(f"capsolver: {r.get('errorCode')} {r.get('errorDescription', '')}")
            if r.get("status") == "ready":
                return r["solution"]["token"]
    raise RuntimeError("capsolver: таймаут")


async def claim(acc: dict, api_key: str) -> str:
    """Запросить 0.1 HYPE на адрес аккаунта. Возвращает итог одной строкой для лога."""
    token = await solve_turnstile(api_key)
    headers = {"Origin": "https://www.alchemy.com", "Referer": PAGE}
    async with AsyncSession(impersonate=IMPERSONATE, proxy=acc["proxy"], timeout=40) as s:
        r = await s.post(API + "/send", json={"address": acc["address"], "turnstileToken": token}, headers=headers)
        try:
            data = r.json()
        except ValueError:
            return f"ошибка: HTTP {r.status_code} {r.text[:150]}"
        tx = data.get("transactionHash")
        if not tx:
            return f"отказ: {data.get('error') or data.get('message') or data}"
        for _ in range(60):
            await asyncio.sleep(2)
            st = (await s.get(API + "/status", params={"transactionHash": tx}, headers=headers)).json()
            if st.get("status") == "success":
                return f"получено 0.1 HYPE, {EXPLORER}{tx}"
            if st.get("status") == "failed":
                return f"транзакция крана не прошла: {st.get('error')}"
        return f"отправлено, но подтверждения не дождался: {EXPLORER}{tx}"


async def main() -> None:
    ap = argparse.ArgumentParser(description="Кран Alchemy: HYPE на HyperEVM testnet для всех аккаунтов")
    ap.add_argument("--limit", type=int, default=None, help="только первые N аккаунтов")
    args = ap.parse_args()
    cfg = load_config()
    load_dotenv(ROOT / ".env")
    api_key = os.getenv("CAPSOLVER_API_KEY", "").strip()
    if not api_key:
        sys.exit("нет CAPSOLVER_API_KEY в .env")
    min_balance = Web3.to_wei(cfg.opt("hyperevm_faucet", "min_balance", 0.05), "ether")
    threads = cfg.opt("hyperevm_faucet", "threads", 5)

    h = Web3(Web3.HTTPProvider(HYPEREVM_RPC, request_kwargs={"timeout": 30}))
    sem = asyncio.Semaphore(threads)

    async def one(acc: dict) -> None:
        tag = f"#{acc['n']} {short(acc['address'])}"
        bal = await asyncio.to_thread(h.eth.get_balance, acc["address"])
        if bal >= min_balance:
            log.info("%s: на HyperEVM уже %s HYPE — пропуск", tag, Web3.from_wei(bal, "ether"))
            return
        async with sem:
            try:
                res = await claim(acc, api_key)
            except Exception as e:
                res = f"ошибка: {e}"
        log.info("%s (%s): кран Alchemy: %s", tag, mask_proxy(acc["proxy"]), res)

    accounts = load_accounts()[: args.limit]
    log.info("кран Alchemy: аккаунтов %d, клейм тем, у кого на HyperEVM < %s HYPE, потоков %d",
             len(accounts), Web3.from_wei(min_balance, "ether"), threads)
    await asyncio.gather(*(one(a) for a in accounts))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("web3", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    asyncio.run(main())
