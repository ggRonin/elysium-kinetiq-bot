"""Веб-панель бота: настройки по проектам, аккаунты и прокси, терминал, кнопки «Пуск» / «Стоп».

Запуск: start.bat (или python webui.py) → откроется браузер на http://127.0.0.1:8765.
Панель только правит config.ini / accounts.txt / Proxy.txt / .env и запускает main.py и служебные скрипты —
логика бота та же, что при запуске из консоли. Сервер слушает только 127.0.0.1 (снаружи недоступен).
"""
from __future__ import annotations

import collections
import os
import re
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.ini"
FILES = {"accounts": ROOT / "accounts.txt", "proxy": ROOT / "Proxy.txt", "x": ROOT / "x_handles.txt"}
ENV = ROOT / ".env"
PAGE = ROOT / "webui" / "index.html"
PY = sys.executable

# скрипты, которые можно запускать из панели: id → (файл, аргументы, слот)
SCRIPTS = {
    "main": ("main.py", [], "main"),
    "points": ("points.py", [], "tool"),
    "hl_faucet": ("hl_faucet.py", [], "tool"),
    "hyperevm_gas": ("hyperevm_gas.py", [], "tool"),
    "hyperevm_gas_dry": ("hyperevm_gas.py", ["--dry"], "tool"),
    "alchemy_faucet": ("alchemy_faucet.py", [], "tool"),
}

# карточки панели: разделы config.ini, ссылки, описание, кнопки скриптов
CARDS = [
    {"id": "general", "title": "Общие", "sections": ["general", "points"],
     "desc": "Потоки, повтор раз в сутки, паузы между действиями, вывод в терминал и таблица поинтов.",
     "links": [], "scripts": [["points", "Поинты сейчас"]]},
    {"id": "faucet", "only": "faucet", "title": "Фосет Elysium — 0.1 HYPE в сутки", "sections": ["faucet"], "env": True,
     "desc": "Официальный фосет. Нужна капча: ключ CapSolver с балансом.",
     "links": [["Фосет", "https://elysium.kinetiq.xyz/testnet-faucet"], ["CapSolver", "https://dashboard.capsolver.com"]]},
    {"id": "bridge", "only": "bridge", "title": "Бридж HYPE Elysium ↔ HyperEVM", "sections": ["bridge"],
     "desc": "Круги вывода в HyperEVM и депозита обратно; клейм подтверждённых выводов.",
     "links": [["Бридж", "https://elysium.kinetiq.xyz/testnet-bridge"]]},
    {"id": "token_bridge", "only": "token_bridge", "title": "Бридж токенов (USDC / WHYPE)", "sections": ["token_bridge"],
     "desc": "Canonical-бридж ERC-20 между Elysium и HyperEVM.",
     "links": [["Бридж", "https://elysium.kinetiq.xyz/testnet-bridge"],
               ["Документация", "https://elysium.kinetiq.xyz/docs/token-bridging-on-elysium"]]},
    {"id": "wrap", "only": "wrap", "title": "Wrap HYPE ↔ WHYPE", "sections": ["wrap"], "desc": "Заворачивание HYPE в WHYPE на Elysium.",
     "links": [["WHYPE в эксплорере",
                "https://elysium.kinetiq.xyz/testnet-explorer/address/0xCD57F65c2B0e5881CFC2E609F7CD53b746E1F234"]]},
    {"id": "swap", "only": "swap", "title": "Dex Da Costa — своп HYPE ↔ USDC", "sections": ["swap"], "desc": "UniswapV2-пул HYPE/USDC.",
     "links": [["Dex Da Costa", "https://dex-da-costa-elysium.vercel.app"]]},
    {"id": "elyra", "only": "elyra", "title": "Elyra — лендинг (поинты)", "sections": ["elyra"],
     "desc": "1 поинт за $1 позиции в час, HYPE ×2, займ ×2, позиции от $1.",
     "links": [["Elyra", "https://useelyra.xyz"], ["Поинты", "https://useelyra.xyz/points"]]},
    {"id": "kalos", "only": "kalos", "title": "Kalos — DEX и NFT (поинты)", "sections": ["kalos"],
     "desc": "Поинты: покупки/продажи NFT, платные минты, чек-ин (свопы токенов не считаются). По поинтам раздаётся KALOS.",
     "links": [["Kalos", "https://www.kalos.fun"], ["Rewards", "https://www.kalos.fun/rewards"]]},
    {"id": "bunnypad", "only": "bunnypad", "title": "BunnyPad — лаунчпад (поинты)", "sections": ["bunnypad"],
     "desc": "Запуск токена +100 (до 10), сделка от $1 +10, объём +1 за $10 (HYPE = $40).",
     "links": [["BunnyPad", "https://www.bunnypad.fun"], ["Поинты", "https://www.bunnypad.fun/points"]]},
    {"id": "perpme", "only": "perpme", "title": "PerpMe — лаунчпад", "sections": ["perpme"], "desc": "Покупка свежего токена.",
     "links": [["PerpMe", "https://perpme.fun/elysium"]]},
    {"id": "games", "only": "games", "title": "Игры: ELYSA.FUN (поинты) и Frog Flip", "sections": ["games"],
     "desc": "Одна ставка в монетку в день. ELYSA: 666 поинтов за 1 HYPE ставки + бонус серии.",
     "links": [["ELYSA.FUN", "https://elysa.fun"], ["Поинты ELYSA", "https://elysa.fun/leaderboard.html"],
               ["Frog Flip", "https://frogflip.xyz"]]},
    {"id": "hunt", "only": "hunt", "title": "Elysium Hunt — квесты (EXP)", "sections": ["hunt"], "desc": "Чек-ин +100 EXP, квест-транзакция +250, квест X +1000 (ники — в блоке аккаунтов), апгрейд карт.",
     "links": [["Elysium Hunt", "https://elysium.archuntx.site"]]},
    {"id": "ely", "only": "ely", "title": ".ely — домены", "sections": ["ely"], "desc": "Один домен на аккаунт, 0.1 HYPE.",
     "links": [[".ely names", "https://elysiumnameservice.xyz"]]},
    {"id": "hl_faucet", "only": "hl_faucet", "title": "Кран Hyperliquid → HYPE в HyperEVM", "sections": ["hl_faucet"],
     "desc": "1000 тестовых USDC в сутки → покупка HYPE → перевод в HyperEVM. Только адреса с аккаунтом в мейннете Hyperliquid.",
     "links": [["Кран", "https://app.hyperliquid-testnet.xyz/drip"]]},
    {"id": "hyperevm_gas", "title": "Газ на HyperEVM для всех (донор)", "sections": ["hyperevm_gas"],
     "desc": "Донор докидывает газ на HyperEVM; каждый аккаунт делает один крупный вывод из Elysium.",
     "links": [], "scripts": [["hyperevm_gas_dry", "Проверить (без отправки)"], ["hyperevm_gas", "Запустить"]]},
    {"id": "hyperevm_faucet", "title": "Кран Alchemy (HyperEVM)", "sections": ["hyperevm_faucet"],
     "desc": "0.1 HYPE / 24 ч, только адресам с 0.01 HYPE в мейннете Hyperliquid. Капча через CapSolver.",
     "links": [["Кран Alchemy", "https://www.alchemy.com/faucets/hyperliquid-testnet"]],
     "scripts": [["alchemy_faucet", "Запустить сейчас"]]},
]

# «Запустить отдельно»: main.py --only <раздел> — один проект, один проход по всем аккаунтам
for _c in CARDS:
    if _c.get("only"):
        SCRIPTS["only:" + _c["only"]] = ("main.py", ["--only", _c["only"]], "main")

# подписи полей: "раздел.ключ" или просто "ключ"
LABELS = {
    "general.threads": "Потоков (аккаунтов одновременно)", "general.loop": "Режим работы",
    "general.pause_min": "Пауза между действиями от, с", "general.pause_max": "Пауза до, с",
    "general.terminal": "Вывод в терминал", "faucet.claim": "Клейм фосета",
    "bridge.bridge": "Режим бриджа", "bridge.bridge_min": "Сумма вывода от, HYPE", "bridge.bridge_max": "Сумма вывода до, HYPE",
    "bridge.bridge_rounds": "Кругов за запуск", "bridge.claim_min": "Не клеймить выводы меньше, HYPE",
    "bridge.hyperevm_reserve": "Резерв газа на HyperEVM, HYPE",
    "bridge.hyperevm_gather": "Собирать HYPE с HyperEVM в Elysium", "bridge.hyperevm_keep": "Оставлять на HyperEVM, HYPE",
    "bridge.hyperevm_gather_min": "Переводить от, HYPE",
    "wrap.wrap": "Режим", "wrap.wrap_min": "Сумма от, HYPE", "wrap.wrap_max": "Сумма до, HYPE", "wrap.wrap_rounds": "Повторов в день",
    "swap.swap": "Режим", "swap.swap_min": "Сумма от, HYPE", "swap.swap_max": "Сумма до, HYPE", "swap.swap_rounds": "Повторов в день",
    "elyra.elyra": "Elyra", "elyra.elyra_faucet": "Фосет Elyra", "elyra.supply_hype_min": "Депозит HYPE от",
    "elyra.supply_hype_max": "Депозит HYPE до", "elyra.supply_usdc_share": "Доля USDC в депозит, %",
    "elyra.elyra_collateral": "ELYRA в залог", "elyra.elyra_borrow": "Займ", "elyra.collateral_min": "Залог HYPE от", "elyra.collateral_max": "Залог HYPE до",
    "elyra.borrow_share": "Займ от лимита, %",
    "token_bridge.tbridge": "Режим", "token_bridge.token": "Токен", "token_bridge.tbridge_min": "Сумма от (в токене)",
    "token_bridge.tbridge_max": "Сумма до (в токене)", "token_bridge.tbridge_rounds": "Выводов в день",
    "token_bridge.tbridge_buy": "Докупать токен",
    "ely.ely": "Регистрация домена", "ely.ely_max_price": "Макс. цена, HYPE",
    "hunt.hunt": "Квесты", "hunt.hunt_mint": "Минт карты", "hunt.hunt_max_price": "Макс. цена карты, HYPE",
    "hunt.hunt_upgrade": "Апгрейд карт", "hunt.hunt_upgrade_to": "Качать до уровня",
    "hunt.hunt_upgrade_max_fee": "Макс. цена попытки, HYPE", "hunt.hunt_upgrade_tries": "Попыток на карту",
    "hunt.hunt_x_quest": "Квест X (Follow @archuntx)", "hunt.ehunt_stake": "Стейк EHUNT (разово)",
    "hunt.ehunt_min": "EHUNT от", "hunt.ehunt_max": "EHUNT до", "hunt.ehunt_max_share": "Пропуск, если дороже % всех HYPE",
    "hyperevm_gas.donor": "Донор (номер строки аккаунта)", "hyperevm_gas.topup": "Докинуть до, HYPE",
    "hyperevm_gas.topup_below": "Если на HyperEVM меньше, HYPE", "hyperevm_gas.big_withdraw": "Крупный вывод из Elysium, HYPE",
    "hyperevm_faucet.min_balance": "Клеймить, если на HyperEVM меньше, HYPE", "hyperevm_faucet.threads": "Потоков",
    "kalos.kalos_swap": "Своп KALOS", "kalos.swap_min": "Сумма от, HYPE", "kalos.swap_max": "Сумма до, HYPE",
    "kalos.kalos_nft": "Минт NFT", "kalos.nft_max_price": "Макс. цена NFT, HYPE", "kalos.kalos_checkin": "Чек-ин",
    "kalos.kalos_claim": "Забирать награду KALOS", "kalos.nft_per_day": "Платных минтов за проход",
    "kalos.kalos_market": "Маркетплейс: купить и выставить NFT", "kalos.market_max_price": "Покупать NFT до, HYPE",
    "kalos.market_markup": "Наценка при выставлении, %",
    "bunnypad.bunnypad": "Сделки", "bunnypad.buy_min": "Сделка от, HYPE", "bunnypad.buy_max": "Сделка до, HYPE",
    "bunnypad.bunny_launch": "Запуск своего токена",
    "perpme.perpme": "Покупка", "perpme.buy_min": "Сумма от, HYPE", "perpme.buy_max": "Сумма до, HYPE",
    "games.elysa": "ELYSA.FUN", "games.elysa_bet": "Ставка ELYSA, HYPE", "games.frogflip": "Frog Flip",
    "games.frog_name": "Ник в Frog Flip", "games.frog_min": "Ставка Frog от, HYPE", "games.frog_max": "Ставка Frog до, HYPE",
    "hl_faucet.hl_faucet": "Кран Hyperliquid", "hl_faucet.keep_usdc": "Оставлять USDC на споте",
    "points.points": "Таблица поинтов", "points.points_first_minutes": "Первая таблица через, мин",
    "points.points_every_hours": "Дальше каждые, ч",
    "slippage": "Проскальзывание, %",
}
SELECTS = {"token": [["USDC", "USDC"], ["WHYPE", "WHYPE"]], "points": [["0", "выключено"], ["1", "включено"]]}

_KEY = re.compile(r"^(\s*)([A-Za-z0-9_]+)(\s*=\s*)([^;#\r\n]*?)(\s*)([;#].*)?$")
_SEC = re.compile(r"^\s*\[([^\]]+)\]\s*$")


# ── config.ini ───────────────────────────────────────────────────────────────
def _write(path: Path, text: str) -> None:
    """Записать, сохранив стиль переводов строк файла (Windows иначе меняет LF на CRLF)."""
    nl = "\r\n" if path.exists() and b"\r\n" in path.read_bytes() else "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline=nl)
    tmp.replace(path)


def _options(comment: str, value: str):
    c = comment.lstrip(";# ").strip()
    opt = r"(?:\d+|[a-z]+)"
    if not re.match(rf"^{opt}\s+—\s", c):
        return None
    opts = [[m.group(1), m.group(2).strip()] for m in
            re.finditer(rf"(?:^|,\s*)({opt})\s+—\s+(.+?)(?=,\s*{opt}\s+—\s|$)", c)]
    if [o[0] for o in opts] == ["1"]:
        opts.insert(0, ["0", "выключено"])
    return opts if value in [o[0] for o in opts] else None


def _label(sec: str, key: str) -> str:
    if f"{sec}.{key}" in LABELS:
        return LABELS[f"{sec}.{key}"]
    if key in LABELS:
        return LABELS[key]
    if key.endswith("_min"):
        return "от"
    if key.endswith("_max"):
        return "до"
    if key.endswith("rounds"):
        return "повторов за проход"
    return key


def read_config() -> dict:
    sections, cur = {}, None
    for line in CONFIG.read_text(encoding="utf-8").splitlines():
        m = _SEC.match(line)
        if m:
            cur = m.group(1).strip()
            sections[cur] = {"desc": [], "fields": []}
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith((";", "#")):
            text = s.lstrip(";# ").strip()
            if text and not text.startswith("──") and not sections[cur]["fields"]:
                sections[cur]["desc"].append(text)
            continue
        m = _KEY.match(line)
        if not m:
            continue
        key, value, comment = m.group(2), m.group(4).strip(), (m.group(6) or "")
        opts = SELECTS.get(key) or _options(comment, value)
        hint = comment.lstrip(";# ").strip()
        sections[cur]["fields"].append({"key": key, "value": value, "label": _label(cur, key),
                                        "hint": "" if opts and key not in SELECTS else hint, "options": opts})
    return sections


def write_config(changes: dict) -> None:
    lines, cur, out = CONFIG.read_text(encoding="utf-8").splitlines(), None, []
    for line in lines:
        m = _SEC.match(line)
        if m:
            cur = m.group(1).strip()
        k = _KEY.match(line) if cur else None
        if k and cur in changes and k.group(2) in changes[cur]:
            new = str(changes[cur][k.group(2)]).strip()
            if re.search(r"[;#\r\n]", new):
                raise ValueError(f"[{cur}] {k.group(2)}: недопустимые символы")
            if k.group(6):  # комментарий остаётся в той же колонке
                width = len(k.group(4)) + len(k.group(5))
                line = f"{k.group(1)}{k.group(2)}{k.group(3)}{new}{' ' * max(1, width - len(new))}{k.group(6)}"
            else:
                line = f"{k.group(1)}{k.group(2)}{k.group(3)}{new}"
        out.append(line)
    _write(CONFIG, "\n".join(out) + "\n")


def read_env() -> str:
    if not ENV.exists():
        return ""
    m = re.search(r"^CAPSOLVER_API_KEY\s*=\s*(.*)$", ENV.read_text(encoding="utf-8"), re.M)
    return m.group(1).strip() if m else ""


def write_env(key: str) -> None:
    if key.strip() == read_env():
        return  # не менялся — файл не трогаем
    text = ENV.read_text(encoding="utf-8") if ENV.exists() else ""
    line = f"CAPSOLVER_API_KEY={key.strip()}"
    text = re.sub(r"^CAPSOLVER_API_KEY\s*=.*$", line, text, flags=re.M) if "CAPSOLVER_API_KEY" in text \
        else (text.rstrip("\n") + "\n" + line + "\n").lstrip("\n")
    _write(ENV, text)


# ── процессы и лог ───────────────────────────────────────────────────────────
LOG: collections.deque = collections.deque(maxlen=6000)
_seq = 0
_lock = threading.Lock()


def log_line(slot: str, text: str) -> None:
    global _seq
    with _lock:
        _seq += 1
        LOG.append({"seq": _seq, "slot": slot, "text": text})


class Slot:
    def __init__(self, name: str):
        self.name, self.proc, self.script, self.started = name, None, None, None

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, script_id: str) -> None:
        if self.running():
            raise RuntimeError("уже запущено: " + self.script)
        file, args, _ = SCRIPTS[script_id]
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.proc = subprocess.Popen([PY, "-u", file, *args], cwd=ROOT, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, creationflags=flags)
        self.script, self.started = script_id, time.time()
        log_line(self.name, f"▶ запуск: {file} {' '.join(args)}".rstrip())
        threading.Thread(target=self._pump, args=(self.proc, file), daemon=True).start()

    def _pump(self, proc, file) -> None:
        for raw in iter(proc.stdout.readline, b""):
            log_line(self.name, raw.decode("utf-8", errors="replace").rstrip("\r\n"))
        code = proc.wait()
        log_line(self.name, f"■ {file} завершён (код {code})")

    def stop(self) -> None:
        if not self.running():
            return
        log_line(self.name, "■ остановка…")
        try:
            if os.name == "nt":
                self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.proc.terminate()
            self.proc.wait(timeout=8)
        except Exception:
            self.proc.kill()


SLOTS = {"main": Slot("main"), "tool": Slot("tool")}


def status() -> dict:
    return {k: {"running": s.running(), "script": s.script if s.running() else None,
                "started": s.started if s.running() else None} for k, s in SLOTS.items()}


# ── HTTP ─────────────────────────────────────────────────────────────────────
def _count(text: str) -> int:
    return sum(1 for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#"))


async def index(_):
    return web.FileResponse(PAGE)


async def get_state(_):
    files = {k: (p.read_text(encoding="utf-8-sig") if p.exists() else "") for k, p in FILES.items()}
    return web.json_response({"cards": CARDS, "config": read_config(), "files": files,
                              "counts": {k: _count(v) for k, v in files.items()},
                              "capsolver": read_env(), "status": status()})


async def save(request):
    data = await request.json()
    if data.get("config"):
        write_config(data["config"])
    for k, p in FILES.items():
        if k in data.get("files", {}):
            text = data["files"][k].replace("\r\n", "\n").strip("\n")
            text = text + ("\n" if text else "")
            old = p.read_text(encoding="utf-8-sig").replace("\r\n", "\n") if p.exists() else ""
            if text != old:  # не менялось — файл не трогаем
                _write(p, text)
    if "capsolver" in data:
        write_env(data["capsolver"])
    return web.json_response({"ok": True})


async def run(request):
    sid = (await request.json()).get("script")
    if sid not in SCRIPTS:
        return web.json_response({"ok": False, "error": "неизвестный скрипт"}, status=400)
    try:
        SLOTS[SCRIPTS[sid][2]].start(sid)
    except RuntimeError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=409)
    return web.json_response({"ok": True, "status": status()})


async def stop(request):
    slot = (await request.json()).get("slot", "main")
    if slot in SLOTS:
        SLOTS[slot].stop()
    return web.json_response({"ok": True, "status": status()})


async def logs(request):
    since = int(request.query.get("since", 0))
    with _lock:
        rows = [r for r in LOG if r["seq"] > since]
    return web.json_response({"lines": rows[-2000:], "status": status()})


def main() -> None:
    app = web.Application()
    app.add_routes([web.get("/", index), web.get("/api/state", get_state), web.post("/api/save", save),
                    web.post("/api/run", run), web.post("/api/stop", stop), web.get("/api/logs", logs)])

    async def on_shutdown(_):
        for s in SLOTS.values():
            s.stop()
    app.on_shutdown.append(on_shutdown)

    import socket
    port = next(p for p in range(8765, 8790) if socket.socket().connect_ex(("127.0.0.1", p)) != 0)
    url = f"http://127.0.0.1:{port}"
    print(f"Панель бота: {url}  (закрой это окно, чтобы выключить панель и бота)", flush=True)
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    web.run_app(app, host="127.0.0.1", port=port, print=None)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
