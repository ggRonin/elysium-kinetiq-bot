"""Собрать HYPE с HyperEVM в Elysium на всех аккаунтах — только это, без кругов бриджа и активностей.

На HyperEVM остаётся hyperevm_keep (газ на клеймы выводов), остальное уходит в Elysium (~1 мин до зачисления).
Настройки — раздел [bridge] в config.ini. Запуск: python gather_hype.py (или кнопка в панели).
"""
from __future__ import annotations

import logging
import sys
from concurrent.futures import ThreadPoolExecutor

import bridge
from common import load_accounts, load_config, log, short


def main() -> None:
    cfg = load_config()
    cfg.ini.set("bridge", "hyperevm_gather", "1")  # запуск вручную = собрать, даже если в ежедневных выключено
    accounts = load_accounts()
    log.info("сбор HYPE с HyperEVM в Elysium: аккаунтов %d, на HyperEVM остаётся %s HYPE", len(accounts),
             cfg.opt("bridge", "hyperevm_keep", 0.02))

    def one(acc: dict) -> None:
        tag = f"#{acc['n']} {short(acc['address'])}"
        try:
            bridge.gather_to_elysium(acc, cfg, tag)
        except Exception as e:
            log.warning("%s: сбор HYPE: ошибка: %s", tag, str(e)[:200])

    with ThreadPoolExecutor(max(1, min(cfg.threads, 8))) as ex:  # не больше 8 потоков: лимит RPC HyperEVM
        list(ex.map(one, accounts))
    log.info("сбор HYPE: готово (депозиты дойдут до Elysium примерно за минуту)")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("web3", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    main()
