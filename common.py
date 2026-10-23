"""Общее: файлы проекта, аккаунты, прокси, конфиг."""
from __future__ import annotations

import configparser
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from eth_account import Account

ROOT = Path(__file__).resolve().parent
ACCOUNTS_FILE = ROOT / "accounts.txt"
PROXY_FILE = ROOT / "Proxy.txt"      # запас: при смерти прокси аккаунта берём отсюда первый и удаляем его
CONFIG_FILE = ROOT / "config.ini"

log = logging.getLogger("elysium")
Account.enable_unaudited_hdwallet_features()


def now() -> datetime:
    return datetime.now(timezone.utc)


def mask_proxy(proxy: str | None) -> str:
    return proxy.rsplit("@", 1)[-1] if proxy else "direct"


def short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


# ── конфиг ───────────────────────────────────────────────────────────────────
@dataclass
class Config:
    threads: int
    loop: bool
    claim: bool
    bridge: int                 # 0 — пропуск, 1 — Elysium → HyperEVM, 2 — туда и обратно
    bridge_min: float
    bridge_max: float
    bridge_rounds: int          # транзакций-кругов бриджа на каждом аккаунте за проход
    claim_min: float = 0.0001   # выводы HYPE меньше этого не клеймятся (газ дороже суммы)
    ini: configparser.ConfigParser | None = None  # весь config.ini: новые разделы читаются через opt()

    def opt(self, sec: str, key: str, default):
        """Значение из config.ini того же типа, что default (int, float, str)."""
        if self.ini is None:
            return default
        raw = self.ini.get(sec, key, fallback=None)
        if raw is None or not raw.strip():
            return default
        raw = raw.strip()
        try:
            if isinstance(default, bool):
                return raw.lower() in ("1", "true", "yes", "on")
            if isinstance(default, int):
                return int(raw)
            if isinstance(default, float):
                return float(raw.replace(",", "."))
        except ValueError:
            raise SystemExit(f"config.ini: [{sec}] {key} = {raw} — ожидается число")
        return raw


def load_config() -> Config:
    cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    if not cp.read(CONFIG_FILE, encoding="utf-8"):
        raise SystemExit(f"нет {CONFIG_FILE.name}")
    num = lambda sec, key, default: cp.get(sec, key, fallback=str(default)).replace(",", ".")
    cfg = Config(
        threads=int(num("general", "threads", 3)),
        loop=cp.getboolean("general", "loop", fallback=False),
        claim=cp.getboolean("faucet", "claim", fallback=True),
        bridge=int(num("bridge", "bridge", 0)),
        bridge_min=float(num("bridge", "bridge_min", 0.0001)),
        bridge_max=float(num("bridge", "bridge_max", 0.0001)),
        bridge_rounds=int(num("bridge", "bridge_rounds", 1)),
        claim_min=float(num("bridge", "claim_min", 0.0001)),
        ini=cp,
    )
    if cfg.bridge not in (0, 1, 2):
        raise SystemExit("bridge в config.ini: 0, 1 или 2")
    if not 0 < cfg.bridge_min <= cfg.bridge_max:
        raise SystemExit("config.ini: нужно 0 < bridge_min <= bridge_max")
    if cfg.bridge_rounds < 1:
        raise SystemExit("config.ini: bridge_rounds должен быть не меньше 1")
    return cfg


# ── аккаунты и прокси ────────────────────────────────────────────────────────
def load_accounts() -> list[dict]:
    """[{n, address, key, proxy}]; n — номер строки в accounts.txt (нужен для замены прокси)."""
    accounts = []
    for n, line in enumerate(ACCOUNTS_FILE.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        mnemonic, _, proxy = line.partition(",")
        try:
            acct = Account.from_mnemonic(mnemonic.strip())
        except Exception as e:
            log.error("строка %d: неверная мнемоника (%s)", n, type(e).__name__)
            continue
        proxy = proxy.strip()
        accounts.append({"n": n, "address": acct.address, "key": acct.key,
                         "proxy": "http://" + proxy if proxy else None})
    return accounts


def _write_lines(path: Path, lines: list[str]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    tmp.replace(path)


def take_proxy() -> str | None:
    """Первый прокси из Proxy.txt; он сразу удаляется из файла, чтобы не достался двум аккаунтам."""
    if not PROXY_FILE.exists():
        return None
    lines = PROXY_FILE.read_text(encoding="utf-8-sig").splitlines()
    for i, line in enumerate(lines):
        if line.strip() and not line.strip().startswith("#"):
            _write_lines(PROXY_FILE, lines[:i] + lines[i + 1:])
            return line.strip()
    return None


def set_account_proxy(n: int, proxy: str) -> None:
    """Заменить прокси в строке n файла accounts.txt (мнемоника не трогается)."""
    lines = ACCOUNTS_FILE.read_text(encoding="utf-8-sig").splitlines()
    mnemonic = lines[n - 1].partition(",")[0].strip()
    lines[n - 1] = f"{mnemonic},{proxy}"
    _write_lines(ACCOUNTS_FILE, lines)
