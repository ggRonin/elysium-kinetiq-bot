"""Оркестратор Elysium testnet: для каждого аккаунта клейм фосета, затем круги бриджа (по config.ini).

    python main.py              # по config.ini
    python main.py --limit 1    # только первые N аккаунтов (проверка)

У каждого аккаунта свой цикл: claim=1 — попытка клейма, если фосет уже готов (время из ответа сервера,
только в памяти), bridge=1/2 — ровно bridge_rounds кругов за запуск (не хватило HYPE — доделает после клейма).
loop=1 — после кругов бот остаётся и клеймит фосет раз в сутки; loop=0 — один проход.
Активности экосистемы (activities.py: фосет Elyra, wrap, swap, Elyra, бридж токенов, .ely, Elysium Hunt) —
сразу после старта, дальше раз в сутки; каждое включается своим разделом config.ini.

Файлы: accounts.txt (мнемоника,прокси), Proxy.txt (запасные прокси), config.ini, .env (CAPSOLVER_API_KEY).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from dotenv import load_dotenv

import activities
import bridge
import faucet
import points
from common import ROOT, Config, load_accounts, load_config, log, now, short

IDLE_PAUSE = 30  # с: сон аккаунта, которому сейчас нечего делать (нет HYPE на круг, клейм не готов)


class Shared:
    """Общее для всех аккаунтов на время работы."""

    def __init__(self, cfg: Config):
        self.sem = asyncio.Semaphore(cfg.threads)   # многопоточность: столько аккаунтов работают одновременно
        self.lock = asyncio.Lock()                  # замена прокси в файлах
        self.faucet_down_until = now()              # фосет пуст / капча не решается — не клеймим до этого времени


async def account_loop(acc: dict, cfg: Config, api_key: str, sh: Shared) -> None:
    """Цикл аккаунта: клейм (если пора) и круги бриджа, пока за этот запуск не сделано bridge_rounds.
    Дальше при loop=1 и claim=1 аккаунт только клеймит фосет раз в сутки; иначе завершается."""
    next_claim = now()
    acts = activities.enabled(cfg)
    next_acts = now()  # активности экосистемы: сразу, потом раз в сутки (+ случайный сдвиг)
    # следующий клейм выводов после того, как круги сделаны; разнесено по времени, чтобы 28 аккаунтов
    # не спрашивали сайт Elysium в одну секунду (он отвечает 502)
    next_outbox = now() + timedelta(hours=1, seconds=random.randint(0, 900))
    done = 0  # кругов бриджа за этот запуск
    while True:
        rounds = 0
        async with sh.sem:
            if cfg.claim and now() >= next_claim and now() >= sh.faucet_down_until:
                try:
                    next_claim = await faucet.claim(acc, api_key, sh.lock)
                except faucet.FaucetDown as e:
                    if now() >= sh.faucet_down_until:
                        what = "капча не решается" if isinstance(e, faucet.CaptchaDown) else "фосет недоступен"
                        log.error("%s (%s): клейм на паузе %d мин", what, e, faucet.RETRY_LATER.seconds // 60)
                    sh.faucet_down_until = now() + faucet.RETRY_LATER
            if acts and now() >= next_acts:
                await asyncio.to_thread(activities.run, acc, cfg)
                next_acts = now() + timedelta(hours=24, minutes=random.randint(5, 90))
                rounds = -1  # что-то сделали: не спать
            if cfg.bridge and done >= cfg.bridge_rounds and now() >= next_outbox:
                # круги сделаны, но выводы продолжают подтверждаться — клеймить раз в час
                await asyncio.to_thread(bridge.claim_confirmed, acc, f"#{acc['n']} {short(acc['address'])}",
                                        cfg.claim_min)
                next_outbox = now() + timedelta(hours=1, seconds=random.randint(0, 300))
            if cfg.bridge and done < cfg.bridge_rounds:
                rounds = await asyncio.to_thread(bridge.run, acc, cfg, cfg.bridge_rounds - done, done)
                done += rounds
                if done >= cfg.bridge_rounds:
                    log.info("#%d %s: бридж: все %d круг(ов) этого запуска сделаны", acc["n"], short(acc["address"]),
                             cfg.bridge_rounds)
        bridge_left = cfg.bridge and done < cfg.bridge_rounds
        if not cfg.loop or not (cfg.bridge or cfg.claim or acts):
            return
        if rounds == 0:
            # делать нечего: ждём клейма / активностей / выводов (что раньше) или просто немного
            wait = IDLE_PAUSE
            if not bridge_left:
                due = [t for t, on in ((max(next_claim, sh.faucet_down_until), cfg.claim), (next_acts, acts),
                                       (next_outbox, bool(cfg.bridge))) if on]
                wait = max(min((min(due) - now()).total_seconds(), 3600), IDLE_PAUSE)
            await asyncio.sleep(wait)


def single_instance():
    """Не дать запустить вторую копию бота (из консоли и из панели одновременно — двойные транзакции)."""
    (ROOT / "logs").mkdir(exist_ok=True)
    f = open(ROOT / "logs" / "main.lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("Бот уже запущен в другом окне (консоль или панель). Сначала останови его.")
    return f  # держим открытым, пока работает бот


async def print_points(accounts: list[dict]) -> None:
    """Таблица поинтов в терминал (print — видна и в режиме terminal = points)."""
    try:
        text = await asyncio.to_thread(points.report, accounts)
    except Exception as e:
        log.warning("поинты: отчёт не собрался: %s", e)
        return
    print(f"\n{now():%Y-%m-%d %H:%M} UTC — поинты (мои / 10-е место лидерборда)\n{text}\n", flush=True)
    log.info("поинты:\n%s", text)


async def points_loop(accounts: list[dict], cfg: Config) -> None:
    """Первый отчёт через points_first_minutes после старта (активности успеют пройти), дальше раз в N часов."""
    await asyncio.sleep(cfg.opt("points", "points_first_minutes", 20) * 60)
    while True:
        await print_points(accounts)
        await asyncio.sleep(cfg.opt("points", "points_every_hours", 6.0) * 3600)


async def main() -> None:
    ap = argparse.ArgumentParser(description="Elysium testnet: фосет + бридж")
    ap.add_argument("--limit", type=int, default=None, help="только первые N аккаунтов")
    ap.add_argument("--only", default=None,
                    help="только один проект (раздел config.ini: faucet, bridge, kalos, elyra, …), один проход")
    args = ap.parse_args()

    cfg = load_config()
    if args.only:
        only_mode(cfg, args.only)
    load_dotenv(ROOT / ".env")
    api_key = os.getenv("CAPSOLVER_API_KEY", "").strip()
    if cfg.claim and not api_key:
        sys.exit("claim=1, но нет CAPSOLVER_API_KEY в .env")

    accounts = load_accounts()[: args.limit]
    mode = {0: "выкл", 1: "Elysium → HyperEVM", 2: "туда и обратно"}[cfg.bridge]
    log.info("аккаунтов: %d | клейм: %s | бридж: %s (%s–%s HYPE, %d круг(ов) за проход) | потоков: %d",
             len(accounts), "да" if cfg.claim else "нет", mode, cfg.bridge_min, cfg.bridge_max,
             cfg.bridge_rounds, cfg.threads)
    on = [name for sec, key, name, _ in activities.ACTIONS if cfg.opt(sec, key, 0)]
    log.info("активности (раз в сутки): %s", ", ".join(dict.fromkeys(on)) or "выкл")

    # бридж работает в потоках (asyncio.to_thread): пул по умолчанию = ядра + 4, а нужно ровно threads
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=cfg.threads))
    sh = Shared(cfg)
    reporter = asyncio.create_task(points_loop(accounts, cfg)) if cfg.opt("points", "points", 1) else None
    results = await asyncio.gather(*(account_loop(a, cfg, api_key, sh) for a in accounts), return_exceptions=True)
    if reporter:
        if cfg.loop:
            await reporter
        else:  # один проход: напечатать итог и выйти
            reporter.cancel()
            await print_points(accounts)
    for acc, res in zip(accounts, results):
        if isinstance(res, Exception):
            log.error("#%d: необработанная ошибка: %r", acc["n"], res)
    if args.only:
        log.info("«%s»: проход по всем аккаунтам завершён", args.only)


def only_mode(cfg: Config, only: str) -> None:
    """Запуск одного проекта: один проход, всё остальное (фосет, бридж, другие проекты, поинты) выключено.
    Настройки самого проекта берутся из config.ini как есть."""
    groups = {"faucet", "bridge"} | {sec for sec, _, _, _ in activities.ACTIONS}
    if only not in groups:
        sys.exit(f"--only {only}: нет такого проекта. Есть: {', '.join(sorted(groups))}")
    cfg.loop = False
    if only != "faucet":
        cfg.claim = False
    if only != "bridge":
        cfg.bridge = 0
    for sec, key, _, _ in activities.ACTIONS:
        if sec != only:
            cfg.ini.set(sec, key, "0")
    if cfg.ini.has_section("points"):
        cfg.ini.set("points", "points", "0")
    enabled = {"faucet": cfg.claim, "bridge": cfg.bridge}.get(
        only, any(cfg.opt(s, k, 0) for s, k, _, _ in activities.ACTIONS if s == only))
    if not enabled:
        sys.exit(f"«{only}»: в настройках проекта всё выключено — включи нужное в его карточке и запусти снова")
    log.info("режим: только «%s», один проход по всем аккаунтам", only)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    _instance_lock = single_instance()
    # полный лог всегда пишется в logs/bot.log; в терминал — всё (terminal = all) или только таблица поинтов
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    (ROOT / "logs").mkdir(exist_ok=True)
    file_h = logging.FileHandler(ROOT / "logs" / "bot.log", encoding="utf-8")
    file_h.setFormatter(fmt)
    handlers = [file_h]
    if load_config().opt("general", "terminal", "points").lower() != "points":
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S"))
        handlers.append(console)
    else:
        print("Бот запущен. В терминале — только таблица поинтов (первая через ~20 мин), "
              "подробный лог: logs/bot.log", flush=True)
    logging.basicConfig(level=logging.INFO, handlers=handlers)
    for noisy in ("web3", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
