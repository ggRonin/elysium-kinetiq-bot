# Elysium testnet бот

Активность в тестнете [Elysium](https://elysium.kinetiq.xyz) (L2 Kinetiq для Hyperliquid) и в проектах его экосистемы.

## Что делает

- Фосет Elysium, бридж HYPE и токенов (USDC / WHYPE) между Elysium и HyperEVM, wrap HYPE ↔ WHYPE.
- Проекты: Kalos (чек-ин, награда KALOS, своп, минт NFT), BunnyPad (запуск токена, сделки), Elyra (лендинг),
  ELYSA.FUN и Frog Flip (ставки), Elysium Hunt (квесты), .ely (домены), Dex Da Costa и PerpMe (свопы).
- Краны: Hyperliquid testnet (USDC → HYPE → HyperEVM), Alchemy; раздача газа на HyperEVM от донора.
- Таблица поинтов «мои / 10-е место лидерборда» по Kalos, BunnyPad, Elyra, ELYSA, Hunt.

## Установка

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy accounts.example.txt accounts.txt
copy Proxy.example.txt Proxy.txt
copy .env.example .env
```

Заполни `accounts.txt`, `Proxy.txt` и ключ CapSolver в `.env` (или прямо в панели).

## Запуск

- **Панель:** `start.bat` → откроется браузер с настройками по проектам, аккаунтами, терминалом и кнопками «Пуск» / «Стоп».
- **Консоль:** `.venv\Scripts\python.exe main.py`
- **Утилиты:** `points.py` (поинты), `hl_faucet.py`, `hyperevm_gas.py` (`--dry` — без отправки), `alchemy_faucet.py`.

Все настройки — в `config.ini`, у каждого параметра есть комментарий.

`accounts.txt`, `Proxy.txt`, `.env` и `logs/` в репозиторий не попадают (`.gitignore`).
