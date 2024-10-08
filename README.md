# FinFlow

**FinFlow** — это проект для учета финансов и капитала, который помогает отслеживать собственные доходы, расходы и инвестиции.

## Установка и запуск проекта

### 1. Клонирование репозитория

Склонируйте репозиторий на свою локальную машину:

```bash
git clone https://github.com/nmashtakov/finflow
cd finflow
```

### 2. Настройка виртуального окружения

Рекомендуется использовать виртуальное окружение для изоляции зависимостей. Для создания виртуального окружения выполните:

```bash
python3 -m venv .venv
source .venv/bin/activate  # Для Windows используйте .venv\Scripts\activate
```

### 3. Установка зависимостей

Установите все необходимые зависимости:

```bash
pip install -r requirements.txt
```

### 4. Запуск проекта с использованием Docker

Проект настроен для запуска в Docker. Чтобы развернуть и запустить проект, выполните следующие команды:

```bash
docker-compose up --build
```

Это создаст и запустит все необходимые контейнеры, такие как база данных и само приложение.

### 5. Остановка проекта

Чтобы остановить и удалить контейнеры, выполните:

```bash
docker-compose down
```

## Использование

После запуска проект будет доступен по адресу `http://localhost:8080`. Используйте его для отслеживания и управления вашими финансами.
