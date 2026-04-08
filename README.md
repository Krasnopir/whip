# whip

Управляй AI-агентами с телефона через Telegram. Лежишь на диване — агент спрашивает разрешения или закончил задачу — нажимаешь кнопку прямо в чате.

```
Агент закончил → в Telegram:

  ✅ Агент закончил

  Сделал рефактор auth модуля, вынес логику в сервис...

  📁 /workspace/myproject

  [🚀 Ебаш дальше]
  [✏️ Написать команду]
  [✅ Стоп, всё готово]

Нажал "Ебаш дальше" → агент продолжает
Написал текст → летит агенту как следующая команда
```

```
Агент хочет выполнить rm -rf / → в Telegram:

  🔧 Разрешить?
  Bash
  $ rm -rf /tmp/old_build

  [✅ Да]  [❌ Нет]
  [🔥 Да на всё в этой сессии]
```

После нажатия кнопки — сообщение редактируется, кнопки скрываются, видно что выбрал.  
В терминале показывает `[whip] ▶ продолжай`.

---

## Быстрый старт

### 1. Создай Telegram бота

1. Напиши [@BotFather](https://t.me/BotFather) → `/newbot` → получи **токен**
2. Напиши [@userinfobot](https://t.me/userinfobot) → получи свой **chat ID**
3. Найди своего бота и напиши ему `/start`

### 2. Установи whip

```bash
git clone https://github.com/Krasnopir/whip.git
cd whip
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

### 3. Настрой

```bash
whip setup
# → введи токен бота
# → введи chat ID
# → автоматически пропишет хуки в Claude Code
```

Или вручную создай `~/.whip/.env`:

```env
WHIP_TELEGRAM_TOKEN=1234567890:ABCdef...
WHIP_TELEGRAM_CHAT_ID=123456789
WHIP_DAEMON_PORT=7331
WHIP_TIMEOUT=1800
```

### 4. Запусти демон

```bash
whip start        # в отдельном терминале
# или в фоне:
whip start -d
```

Всё. Теперь запускай Claude Code в любом проекте — управление прилетает в телефон.

---

## Как работает

```
Claude Code
   │ Stop hook (агент закончил)
   │ PreToolUse hook (агент хочет запустить команду)
   ▼
whip daemon (localhost:7331)   ← FastAPI, крутится локально
   │
   ▼ Telegram Bot API (long-polling, без webhook)
Твой телефон
```

**Stop hook** — когда Claude Code заканчивает задачу, перехватывает момент и шлёт резюме в Telegram. Держит соединение открытым пока не нажмёшь кнопку (до 30 мин). Если нажал "Ебаш дальше" — агент получает команду и продолжает. Написал текст — летит как следующая инструкция.

**PreToolUse hook** — перехватывает потенциально опасные bash-команды (`rm`, `git push`, `git reset --hard`, `sudo`, и т.д.). "Да на всё" — включает режим автоапрува до конца сессии.

**Никакого облака** — всё локально, Telegram только как транспорт.

---

## Команды

```bash
whip setup          # настройка + установка хуков в Claude Code
whip start          # запустить демон (foreground)
whip start -d       # запустить в фоне
whip stop-daemon    # остановить фоновый демон
whip status         # проверить что демон жив
whip notify "текст" # отправить сообщение вручную (работает с любым агентом)
whip notify "готово" -b "Продолжить" -b "Стоп"  # с кнопками
```

---

## Интеграция с Codex и другими агентами

Для любого агента без встроенных хуков используй `whip notify`:

```bash
# После завершения задачи
codex "сделай тудулист" && whip notify "Codex закончил!" -b "Ебаш дальше" -b "Стоп"
```

Или в скрипте:

```bash
#!/bin/bash
your-agent "$@"
whip notify "Агент закончил задачу: $1" -b "Продолжить" -b "Стоп"
```

---

## Автозапуск (macOS)

Чтобы демон стартовал сам при входе в систему, добавь в `~/.zshrc`:

```bash
pgrep -f "uvicorn whip.daemon" > /dev/null || \
  nohup /path/to/whip/.venv/bin/python \
  -m uvicorn whip.daemon:app --host 127.0.0.1 --port 7331 \
  >> ~/.whip/daemon.log 2>&1 &
```

---

## Конфиг

| Переменная | По умолчанию | Описание |
|---|---|---|
| `WHIP_TELEGRAM_TOKEN` | — | Токен бота от BotFather |
| `WHIP_TELEGRAM_CHAT_ID` | — | Твой chat ID |
| `WHIP_DAEMON_PORT` | `7331` | Порт демона |
| `WHIP_TIMEOUT` | `1800` | Таймаут ожидания ответа (сек) |

---

## License

MIT
