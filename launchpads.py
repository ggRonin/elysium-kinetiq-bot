"""Лаунчпады Elysium: покупка свежих токенов за HYPE.

BunnyPad (bunnypad.fun): BunnyLaunchpad.buy(token, 0, minOut, to) с value в HYPE; при bunnypad = 2 — сразу продаёт обратно.
    Поинты BunnyPad (bunnypad.fun/points, считаются по событиям в сети, снапшот перед мейннетом):
    запуск токена +100 (первые 10, за первый ещё +50); сделка от $1 +10, первая покупка +25, первая продажа +25;
    объём +1 за $10. Цены фиксированы: HYPE = $40, значит сделка должна быть от 0.025 HYPE.
    Не больше 30 засчитанных сделок на кошелёк в одном токене.
PerpMe (perpme.fun/elysium): токены торгуются в их Uniswap V3 (свой роутер и wrapped HYPE), пул с комиссией 1%.

Какой токен купить, бот решает по свежим успешным покупкам в сети (события через RPC, без эксплорера): берёт случайный из тех,
что сейчас реально покупают, и проверяет покупку симуляцией. Минимум на выходе — результат симуляции минус slippage.
"""
from __future__ import annotations

import random
import time
from collections import Counter

from common import Config, log
from onchain import (E, call, contract, cs, ensure_allowance, erc20_balance, fmt, pause, rand_amount,
                     recent_calls, send_tx, simulate, topic_addr, w3, with_retry)

BUNNY_LP = "0x3c4F47959101557Eed9Bc4ae8D8D986aE8d3E5b1"
PERP_ROUTER = "0xD218817cfCcA3Ae397dE3a6E66889D4Fb4b9e2D1"
PERP_WETH = "0x757A34dD7b411546c6dD8F510B52E4381102f9B0"
PERP_FEE = 10000

_A = lambda n: {"name": n, "type": "address"}
_U = lambda n: {"name": n, "type": "uint256"}
BUNNY_ABI = [
    {"type": "function", "name": "buy", "stateMutability": "payable",
     "inputs": [_A("token"), _U("amountIn"), _U("minTokensOut"), _A("to")], "outputs": [_U("out")]},
    {"type": "function", "name": "sell", "stateMutability": "nonpayable",
     "inputs": [_A("token"), _U("tokensIn"), _U("minQuoteOut"), _A("to")], "outputs": [_U("out")]},
]
_P = {"name": "params", "type": "tuple", "components": [
    _A("tokenIn"), _A("tokenOut"), {"name": "fee", "type": "uint24"}, _A("recipient"), _U("deadline"),
    _U("amountIn"), _U("amountOutMinimum"), {"name": "sqrtPriceLimitX96", "type": "uint160"}]}
PERP_ABI = [{"type": "function", "name": "exactInputSingle", "stateMutability": "payable", "inputs": [_P],
             "outputs": [_U("amountOut")]}]


BUNNY_CREATE_ABI = [
    {"type": "function", "name": "createToken", "stateMutability": "payable",
     "inputs": [{"name": "p", "type": "tuple", "components": [
         {"name": "name", "type": "string"}, {"name": "symbol", "type": "string"},
         {"name": "metadata", "type": "string"}, _A("quote"), _A("feeRecipient"),
         {"name": "holderMode", "type": "bool"}, {"name": "buyTaxBps", "type": "uint16"},
         {"name": "sellTaxBps", "type": "uint16"}, _U("firstBuy"), _U("minTokensOut")]}],
     "outputs": [_A("token")]},
    {"type": "function", "name": "launchFee", "stateMutability": "view", "inputs": [], "outputs": [_U("fee")]},
]
BUNNY_MAX_LAUNCHES = 10  # больше 10 запусков на кошелёк поинтов не дают
# событие запуска токена BunnyLaunchpad: topics = [это, token, creator, quote]
BUNNY_LAUNCH_TOPIC = "0x00aa4e081030a9d4e5022596b3e7554c97456e4d5a055b8ea86d09950d365467"
BUNNY_FROM_BLOCK = 11_518_824  # деплой BunnyLaunchpad (как BUNNY_FROM в points.py)
# Deposit(address indexed dst, uint256 wad) у wrapped HYPE PerpMe: так видно покупки за HYPE через их роутер
WETH_DEPOSIT_TOPIC = "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c"
_W1 = ["Moon", "Hype", "Lucky", "Turbo", "Neon", "Pixel", "Cosmic", "Wild", "Happy", "Golden", "Frosty", "Rocket",
       "Shiny", "Sleepy", "Mega", "Tiny", "Brave", "Silly", "Hyper", "Elysian"]
_W2 = ["Bunny", "Frog", "Cat", "Pepe", "Otter", "Panda", "Fox", "Duck", "Whale", "Koala", "Llama", "Shark",
       "Owl", "Hamster", "Tiger", "Penguin", "Dragon", "Carrot", "Wolf", "Moose"]
_COLORS = ["#A3E4CC", "#F6C177", "#EB6F92", "#9CCFD8", "#C4A7E7", "#F2CDCD", "#94E2D5", "#FAB387"]


def _launch_meta() -> tuple[str, str, str]:
    """Случайные название, тикер и metadata (описание + простая SVG-картинка с тикером)."""
    import base64
    import json
    name = f"{random.choice(_W1)} {random.choice(_W2)}"
    symbol = "".join(w[:2] for w in name.split()).upper() + str(random.randint(1, 99))
    bg, fg = random.sample(_COLORS, 2)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" '
           f'rx="48" fill="{bg}"/><circle cx="128" cy="112" r="64" fill="{fg}"/><text x="128" y="224" '
           f'font-family="Arial" font-size="40" font-weight="bold" text-anchor="middle" fill="#0E1512">{symbol}</text></svg>')
    image = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
    meta = json.dumps({"description": f"{name} on Elysium", "image": image}, separators=(",", ":"))
    return name, symbol, meta


def _my_launches(acc: dict) -> int:
    """Сколько токенов этот кошелёк уже запустил на BunnyPad: события запуска в сети, где создатель — он."""
    c = w3(E[0])
    logs = with_retry(c.eth.get_logs, {"fromBlock": BUNNY_FROM_BLOCK, "toBlock": "latest", "address": cs(BUNNY_LP),
                                       "topics": [BUNNY_LAUNCH_TOPIC, None, topic_addr(acc["address"])]})
    return len(logs)


def bunnypad_launch(acc: dict, cfg: Config, tag: str) -> None:
    """Запустить свой токен на BunnyPad: +100 поинтов (первый +150), считаются первые 10 запусков."""
    if not cfg.opt("bunnypad", "bunny_launch", 0):
        return
    done = _my_launches(acc)
    if done >= BUNNY_MAX_LAUNCHES:
        log.info("%s: bunnypad: уже %d запусков — больше поинтов не дают", tag, done)
        return
    lp = contract(E, BUNNY_LP, BUNNY_CREATE_ABI, acc)
    fee = call(lp.functions.launchFee())
    name, symbol, meta = _launch_meta()
    p = (name, symbol, meta, "0x" + "0" * 40, "0x" + "0" * 40, False, 0, 0, 0, 0)
    token = simulate(lp.functions.createToken(p), acc, fee)
    link = send_tx(E, acc, BUNNY_LP, lp.encode_abi("createToken", args=[p]), value=fee)
    log.info("%s: bunnypad: запущен токен %s ($%s) %s за %s HYPE, запуск %d/%d, %s", tag, name, symbol, token,
             fmt(fee), done + 1, BUNNY_MAX_LAUNCHES, link)


def _candidates(tokens: list[str], top: int = 8) -> list[str]:
    """Самые покупаемые токены, в случайном порядке."""
    best = [t for t, _ in Counter(cs(t) for t in tokens).most_common(top)]
    random.shuffle(best)
    return best


def bunnypad(acc: dict, cfg: Config, tag: str) -> None:
    mode = cfg.opt("bunnypad", "bunnypad", 0)
    if not mode:
        return
    me = acc["address"]
    lp = contract(E, BUNNY_LP, BUNNY_ABI, acc)
    slip = cfg.opt("bunnypad", "slippage", 5.0) / 100
    rounds = max(int(cfg.opt("bunnypad", "rounds", 1)), 1)
    # в одном токене засчитываются не больше 30 сделок — каждый круг берём другой токен
    tokens = _candidates([c["params"]["token"] for c in recent_calls(BUNNY_LP, "buy", BUNNY_ABI)],
                         top=max(8, rounds * 2))
    done = 0
    for token in tokens:
        if done >= rounds:
            break
        amt = rand_amount(cfg.opt("bunnypad", "buy_min", 0.0005), cfg.opt("bunnypad", "buy_max", 0.001))
        try:
            out = simulate(lp.functions.buy(token, 0, 0, me), acc, amt)
        except Exception as e:
            log.debug("%s: bunnypad: %s не покупается: %s", tag, token, e)
            continue
        before = erc20_balance(E, token, me, acc)
        try:
            link = send_tx(E, acc, BUNNY_LP, lp.encode_abi("buy", args=[token, 0, int(out * (1 - slip)), me]),
                           value=amt)
        except Exception as e:
            if "отклонена" in str(e):  # цену сдвинули чужие покупки
                log.info("%s: bunnypad: цена %s ушла, пробую другой токен", tag, token)
                continue
            raise
        got = erc20_balance(E, token, me, acc) - before
        done += 1
        log.info("%s: bunnypad: круг %d/%d — купил токен %s за %s HYPE, %s", tag, done, rounds, token, fmt(amt), link)
        if mode == 2 and got > 0:
            pause(cfg)
            ensure_allowance(E, acc, token, BUNNY_LP, got, tag, "bunnypad")
            back = simulate(lp.functions.sell(token, got, 0, me), acc)
            link = send_tx(E, acc, BUNNY_LP, lp.encode_abi("sell", args=[token, got, int(back * (1 - slip)), me]))
            log.info("%s: bunnypad: продал обратно за %s HYPE, %s", tag, fmt(back), link)
        if done < rounds:
            pause(cfg)
    if not done:
        log.info("%s: bunnypad: не нашёл токен, который сейчас можно купить", tag)


def perpme(acc: dict, cfg: Config, tag: str) -> None:
    if not cfg.opt("perpme", "perpme", 0):
        return
    me = acc["address"]
    r = contract(E, PERP_ROUTER, PERP_ABI, acc)
    slip = cfg.opt("perpme", "slippage", 5.0) / 100
    amt = rand_amount(cfg.opt("perpme", "buy_min", 0.0005), cfg.opt("perpme", "buy_max", 0.001))
    tokens = []
    for c in recent_calls(PERP_ROUTER, "exactInputSingle", PERP_ABI, log_address=PERP_WETH,
                          topics=[WETH_DEPOSIT_TOPIC, topic_addr(PERP_ROUTER)]):
        p = c["params"]["params"]
        if cs(p[0]) == cs(PERP_WETH):  # покупки за HYPE
            tokens.append(p[1])
    for token in _candidates(tokens):
        params = (cs(PERP_WETH), token, PERP_FEE, me, int(time.time()) + 600, amt, 0, 0)
        try:
            out = simulate(r.functions.exactInputSingle(params), acc, amt)
        except Exception as e:
            log.debug("%s: perpme: %s не покупается: %s", tag, token, e)
            continue
        params = params[:6] + (int(out * (1 - slip)), 0)
        try:
            link = send_tx(E, acc, PERP_ROUTER, r.encode_abi("exactInputSingle", args=[params]), value=amt)
        except Exception as e:
            if "отклонена" in str(e) or "too little received" in str(e).lower():  # цену сдвинули другие покупки
                log.info("%s: perpme: цена %s ушла, пробую другой токен", tag, token)
                continue
            raise
        log.info("%s: perpme: купил токен %s за %s HYPE, %s", tag, token, fmt(amt), link)
        return
    log.info("%s: perpme: не нашёл токен, который сейчас можно купить", tag)
