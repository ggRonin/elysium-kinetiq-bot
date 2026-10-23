"""Фосет Elysium testnet: 0.1 HYPE на аккаунт раз в сутки, только HTTP-запросами.
Бот ничего не хранит в файлах: просто пробует, а сервер отвечает транзакцией или кулдауном.

Проход: страница фосета -> Turnstile (CapSolver) -> свежий c.js -> botid.js (x-is-human) -> POST /api/faucet/testnet.
Каждый аккаунт ходит через свой прокси: кулдаун фосета считается и по адресу, и по IP.
Если прокси аккаунта не отвечает 2 раза подряд, берётся первый из Proxy.txt (и удаляется оттуда),
а в accounts.txt он записывается вместо старого.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

from common import ROOT, log, mask_proxy, now, set_account_proxy, short, take_proxy

BOTID_JS = ROOT / "botid.js"

BASE = "https://elysium.kinetiq.xyz"
PAGE = BASE + "/testnet-faucet"
API_PATH = "/api/faucet/testnet"
CJS_PATH = "/149e9513-01fa-4fb0-aad4-566afd725d1b/2d206a39-8ed7-437e-a3be-862e0f06eea3/a-4-a/c.js"
TURNSTILE_KEY = "0x4AAAAAAFItMMT0n2NGsM3v"
EXPLORER_TX = BASE + "/testnet-explorer/transaction/"

# User-Agent обязан совпадать с TLS-профилем curl_cffi
IMPERSONATE = "chrome136"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"

COOLDOWN = timedelta(hours=24, minutes=1)
RETRY_LATER = timedelta(minutes=30)
MAX_ATTEMPTS = 3      # на аккаунт за проход: bot/captcha/failed/прочие ошибки
PROXY_FAILS = 2       # столько сбоев прокси подряд — и он считается мёртвым
MAX_PROXY_SWAPS = 3   # замен прокси на аккаунт за проход
SUBMIT_GAP = 2.0      # пауза между отправками на фосет, с

# Фосет шлёт HYPE с одного кошелька: одновременные запросы валят друг друга по nonce (ответ "failed").
# Поэтому капча решается параллельно, а c.js -> BotID -> POST идут строго по одному.
_submit_lock = asyncio.Lock()


class FaucetDown(Exception):
    """Фосет пуст или выключен: дальше в этом проходе идти бессмысленно."""


# Ошибки CapSolver, после которых решать капчу бесполезно, пока человек не пополнит баланс / не сменит ключ
CAPTCHA_FATAL = {"ERROR_ZERO_BALANCE", "ERROR_KEY_DENIED_ACCESS", "ERROR_INVALID_TASK_DATA_KEY",
                 "ERROR_KEY_DOES_NOT_EXIST", "ERROR_WRONG_USER_KEY", "ERROR_ACCOUNT_SUSPENDED"}


class CaptchaDown(FaucetDown):
    """CapSolver без баланса или с неверным ключом: клейм останавливается до следующего прохода."""


def _capsolver_error(r: dict) -> Exception:
    code = r.get("errorCode", "")
    msg = f"capsolver: {code} {r.get('errorDescription', '')}".strip()
    return CaptchaDown(msg) if code in CAPTCHA_FATAL else RuntimeError(msg)


class ProxyDead(Exception):
    """Прокси не соединяется, не отвечает или отказывает в авторизации."""


# ── внешние решатели ─────────────────────────────────────────────────────────
async def solve_turnstile(api_key: str) -> str:
    async with AsyncSession() as s:
        r = (await s.post("https://api.capsolver.com/createTask", json={
            "clientKey": api_key,
            "task": {"type": "AntiTurnstileTaskProxyLess", "websiteURL": PAGE, "websiteKey": TURNSTILE_KEY},
        }, timeout=30)).json()
        if r.get("errorId"):
            raise _capsolver_error(r)
        task_id = r["taskId"]
        for _ in range(40):
            await asyncio.sleep(3)
            r = (await s.post("https://api.capsolver.com/getTaskResult",
                              json={"clientKey": api_key, "taskId": task_id}, timeout=30)).json()
            if r.get("errorId"):
                raise _capsolver_error(r)
            if r.get("status") == "ready":
                return r["solution"]["token"]
    raise RuntimeError("capsolver: таймаут")


async def solve_botid(cjs: bytes) -> str:
    p = await asyncio.create_subprocess_exec(
        "node", str(BOTID_JS), UA,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(p.communicate(cjs), timeout=30)
    if p.returncode != 0:
        raise RuntimeError(f"botid.js: {err.decode(errors='replace').strip() or p.returncode}")
    challenge = json.loads(out)
    if challenge.get("d") == 1:
        log.warning("BotID запросил Deep Analysis (d=1): запросами это не пройти")
    return json.dumps(challenge, separators=(",", ":"))


# ── один аккаунт ─────────────────────────────────────────────────────────────
async def _send(s: AsyncSession, method: str, url: str, **kw):
    """Запрос к сайту через прокси аккаунта; сетевые сбои и 407 превращаются в ProxyDead."""
    try:
        r = await s.request(method, url, **kw)
    except RequestException as e:
        raise ProxyDead(f"{type(e).__name__}: {str(e)[:120]}") from e
    if r.status_code == 407:
        raise ProxyDead("HTTP 407: прокси отказал в авторизации")
    return r


async def claim_once(acc: dict, api_key: str) -> dict:
    """Одна попытка. Возвращает JSON-ответ фосета ({transactionHash} или {error, retryAt?})."""
    async with AsyncSession(impersonate=IMPERSONATE, proxy=acc["proxy"], timeout=40) as s:
        common = {"user-agent": UA, "accept-language": "en-US,en;q=0.9"}
        r = await _send(s, "GET", PAGE, headers=common)
        if r.status_code != 200:
            raise RuntimeError(f"страница фосета: HTTP {r.status_code}")

        token = await solve_turnstile(api_key)

        async with _submit_lock:
            # c.js берём прямо перед отправкой: его ответ одноразовый и привязан ко времени
            r = await _send(s, "GET", BASE + CJS_PATH, params={"i": "0", "v": "3", "h": "elysium.kinetiq.xyz"},
                            headers={**common, "referer": PAGE, "sec-fetch-dest": "script"})
            if r.status_code != 200:
                raise RuntimeError(f"c.js: HTTP {r.status_code}")
            is_human = await solve_botid(r.content)

            r = await _send(s, "POST", BASE + API_PATH, json={"address": acc["address"], "captchaToken": token}, headers={
                **common, "origin": BASE, "referer": PAGE,
                "x-is-human": is_human, "x-path": API_PATH, "x-method": "POST",
            })
            await asyncio.sleep(SUBMIT_GAP)
        try:
            return r.json()
        except Exception:
            raise RuntimeError(f"фосет: HTTP {r.status_code} {r.text[:120]!r}")


async def claim(acc: dict, api_key: str, lock: asyncio.Lock) -> datetime:
    """Клейм с повторами и заменой прокси. Возвращает, когда фосет снова даст этому аккаунту
    (по ответу сервера). Бросает FaucetDown, если фосет пуст/выключен или не решается капча."""
    addr = acc["address"]

    def tag() -> str:
        return f"#{acc['n']} {short(addr)} via {mask_proxy(acc['proxy'])}"

    attempt = proxy_fails = swaps = 0
    while attempt < MAX_ATTEMPTS:
        try:
            reply = await claim_once(acc, api_key)
            proxy_fails = 0
        except ProxyDead as e:
            # сбои прокси не тратят попытки: после PROXY_FAILS подряд меняем прокси из Proxy.txt
            proxy_fails += 1
            log.warning("%s: прокси не отвечает (%d/%d): %s", tag(), proxy_fails, PROXY_FAILS, e)
            if proxy_fails < PROXY_FAILS:
                await asyncio.sleep(5)
                continue
            if swaps >= MAX_PROXY_SWAPS:
                reply = {"error": "proxy", "detail": f"сменили прокси {swaps} раз, все не работают"}
            else:
                async with lock:
                    new = take_proxy()
                    if new:
                        set_account_proxy(acc["n"], new)
                if not new:
                    log.error("%s: Proxy.txt пуст, заменить прокси нечем", tag())
                    reply = {"error": "proxy", "detail": "Proxy.txt пуст"}
                else:
                    old = mask_proxy(acc["proxy"])
                    acc["proxy"] = "http://" + new
                    swaps += 1
                    proxy_fails = 0
                    log.info("#%d %s: прокси %s заменён на %s (accounts.txt обновлён)",
                             acc["n"], short(addr), old, mask_proxy(new))
                    continue
            attempt = MAX_ATTEMPTS - 1  # прокси не починить: сразу к откладыванию аккаунта
        except FaucetDown:
            raise
        except Exception as e:
            reply = {"error": "exception", "detail": str(e)}
        attempt += 1
        err = reply.get("error")

        if "transactionHash" in reply:
            log.info("%s: фосет прислал 0.1 HYPE, %s%s", tag(), EXPLORER_TX, reply["transactionHash"])
            return now() + COOLDOWN
        if err == "cooldown":
            retry = reply.get("retryAt")
            when = datetime.fromisoformat(retry.replace("Z", "+00:00")) if retry else now() + COOLDOWN
            log.info("%s: фосет на кулдауне до %s UTC", tag(), when.strftime("%Y-%m-%d %H:%M"))
            return when
        if err == "invalid_address":
            log.error("%s: адрес отклонён фосетом", tag())
            return now() + timedelta(days=365)
        if err in ("empty", "unavailable"):
            raise FaucetDown(err)

        # bot / captcha / failed / proxy / exception: повторяем, потом откладываем
        detail = f" ({reply['detail']})" if reply.get("detail") else ""
        log.warning("%s: фосет, попытка %d: %s%s", tag(), attempt, err, detail)
        if attempt == MAX_ATTEMPTS:
            return now() + RETRY_LATER
        await asyncio.sleep(5)
    return now() + RETRY_LATER
