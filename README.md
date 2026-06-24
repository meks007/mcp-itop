# mcp-itop

MCP-сервер для **iTop ITSM** — аналитика, заявки, комментарии, база знаний, CI.

Предоставляет AI-ассистентам (opencode, Claude Desktop, Cursor) **19 инструментов** для работы с iTop:
SLA-аналитика, нагрузка агентов, качество услуг, жизненный цикл заявок, поиск по БЗ, impact-анализ CI.

## Возможности

### Аналитика
| Инструмент | Описание |
|------------|----------|
| `itop_sla_report` | SLA-отчёт по услуге за период (TTO/TTR passed/breached/N/A, медиана решения) |
| `itop_agent_workload` | Загрузка агентов: закрытые/открытые заявки, time_spent, backlog |
| `itop_idle_agents` | Поиск заявок, где агент бездействует >N часов без действий |
| `itop_service_quality` | Поиск похожих заявок, назначенных на разные услуги |
| `itop_caller_quality` | Качество выбора услуг пользователями |
| `itop_agent_correction_rate` | Агенты, которые исправляют / не исправляют услуги |
| `itop_ticket_summary` | Дашборд: создано/решено/открыто/SLA breaches |

### Комментарии
| Инструмент | Описание |
|------------|----------|
| `itop_add_comment` | Добавить публичный или приватный комментарий к заявке |
| `itop_get_log` | Прочитать историю комментариев (public_log, private_log) |

### База знаний
| Инструмент | Описание |
|------------|----------|
| `itop_search_kb` | Поиск статей БЗ (поддерживает KBEntry и FAQ) |
| `itop_get_kb_article` | Полный текст статьи |
| `itop_list_kb_categories` | Список рубрик БЗ |

### CRUD + Жизненный цикл
| Инструмент | Описание |
|------------|----------|
| `itop_get` | Поиск объектов (OQL / ID / JSON-критерии) |
| `itop_create` | Создание объекта |
| `itop_update` | Обновление полей объекта |
| `itop_delete` | Удаление с simulate-режимом |
| `itop_apply_stimulus` | Жизненный цикл: ev_assign, ev_resolve, ev_close, ev_reopen |
| `itop_get_related` | Impact-анализ CI (impacts/depends on) |
| `itop_describe_class` | Разведка полей класса по существующему объекту |

## Быстрый старт

### 1. Установка

```bash
pip install mcp[fastmcp] httpx python-dotenv
```

### 2. Настройка (глобальный конфиг)

```bash
mkdir -p ~/.config/mcp-itop
cat > ~/.config/mcp-itop/.env << 'CONFIG'
ITOP_URL=https://your-itop.example.com
# Токен или логин+пароль:
ITOP_TOKEN=ваш_токен_здесь
# ITOP_USER=admin
# ITOP_PASSWORD=secret
ITOP_VERSION=1.3
ITOP_VERIFY_SSL=true
ITOP_TIMEOUT=30
CONFIG
```

### 3. Запуск

```bash
python server.py
```

## Интеграция

### opencode (глобальный конфиг)

Добавить в `~/.config/opencode/opencode.json`:

```json
"itop": {
  "type": "local",
  "command": ["python", "/путь/до/mcp-itop/server.py"],
  "enabled": true
}
```

### opencode (на проект)

Добавить в `opencode.json` проекта:

```json
{
  "mcpServers": {
    "itop": {
      "command": "python",
      "args": ["/путь/до/mcp-itop/server.py"],
      "env": {
        "ITOP_URL": "https://your-itop.example.com",
        "ITOP_TOKEN": "ваш_токен"
      }
    }
  }
}
```

### Claude Desktop

Добавить в `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "itop": {
      "command": "python",
      "args": ["/путь/до/mcp-itop/server.py"]
    }
  }
}
```

## Примеры запросов

```
Покажи SLA по услуге "Техподдержка" за этот месяц
Кто из агентов перегружен?
Какие заявки висят без движения больше 2 часов?
Найди похожие заявки с разными услугами
Кто из пользователей часто выбирает не ту услугу?
Добавь комментарий к заявке RQ-123
Создай новую заявку: Не работает принтер
Назначь RQ-456 на Иванова
Найди CI, связанные с сервером srv-web-01
Поищи в БЗ по VPN
```

## Совместимость

Протестировано на:

- **iTop** 3.2.1-1-16749 (PHP 8.1.2, MariaDB 10.6)
- Поддерживает русскую локаль (да/нет для SLA) и английскую (true/false)
- Автоопределение модуля БЗ: KBEntry → FAQ

## Требования

- Python ≥ 3.10
- `mcp[fastmcp]`
- `httpx`
- `python-dotenv`

## Тесты

```bash
python -m pytest tests/ -v
```

## Архитектура

```
AI-клиент → MCP (stdio) → server.py → iTop REST API
```

Приоритет конфигурации:
1. `~/.config/mcp-itop/.env` (глобальный, наивысший приоритет)
2. `.env` (локальный в папке проекта)
3. Переменные окружения

## Лицензия

MIT
