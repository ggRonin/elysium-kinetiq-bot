"""Активности в экосистеме Elysium, по одному проходу на аккаунт раз в сутки:
фосет Elyra → wrap → swap → депозиты Elyra → бридж токенов → домен .ely → квесты Elysium Hunt →
Kalos (чек-ин, награда KALOS, своп, минт NFT) → BunnyPad (запуск токена, сделки) → PerpMe → ставки ELYSA.FUN и Frog Flip.

Каждое действие включается своим разделом config.ini. Ошибка одного действия не мешает остальным.
"""
from __future__ import annotations

import bridge
import elyra
import games
import hl_faucet
import hunt
import kalos
import launchpads
import names
import swap
import tbridge
import wrap
from common import Config, log, short
from onchain import NoFunds, pause

# (раздел config.ini, ключ включения, название в логе, функция)
ACTIONS = [
    ("bridge", "hyperevm_gather", "сбор HYPE в Elysium", bridge.gather_to_elysium),
    ("hl_faucet", "hl_faucet", "кран Hyperliquid", hl_faucet.run),
    ("elyra", "elyra", "фосет Elyra", elyra.faucet),
    ("wrap", "wrap", "wrap", wrap.run),
    ("swap", "swap", "swap", swap.run),
    ("elyra", "elyra", "Elyra", elyra.run),
    ("token_bridge", "tbridge", "бридж токенов", tbridge.run),
    ("ely", "ely", ".ely", names.run),
    ("hunt", "hunt", "Elysium Hunt", hunt.run),
    ("hunt", "ehunt_stake", "EHUNT стейк", hunt.ehunt_stake),
    ("kalos", "kalos_checkin", "Kalos чек-ин", kalos.checkin),
    ("kalos", "kalos_claim", "Kalos награда", kalos.claim_rewards),
    ("kalos", "kalos_swap", "Kalos swap", kalos.swap),
    ("kalos", "kalos_nft", "Kalos NFT", kalos.nft),
    ("bunnypad", "bunny_launch", "BunnyPad запуск", launchpads.bunnypad_launch),
    ("bunnypad", "bunnypad", "BunnyPad", launchpads.bunnypad),
    ("perpme", "perpme", "PerpMe", launchpads.perpme),
    ("games", "elysa", "ELYSA.FUN", games.elysa),
    ("games", "frog_name", "Frog Flip ник", games.frog_name),
    ("games", "frogflip", "Frog Flip", games.frogflip),
]


def enabled(cfg: Config) -> bool:
    return any(cfg.opt(sec, key, 0) for sec, key, _, _ in ACTIONS)


def run(acc: dict, cfg: Config) -> None:
    tag = f"#{acc['n']} {short(acc['address'])}"
    for sec, key, name, fn in ACTIONS:
        if not cfg.opt(sec, key, 0):
            continue
        try:
            fn(acc, cfg, tag)
        except NoFunds as e:
            log.info("%s: %s: мало HYPE: %s", tag, name, e)
        except Exception as e:
            log.warning("%s: %s: ошибка: %s", tag, name, str(e)[:300])
        pause(cfg)
