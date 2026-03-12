# Setup Guide — Firebase Alarm Mesh

Инструкция для нового экземпляра Claude по настройке серверов.

## Структура проекта

```
config/machines.json      — реестр всех машин (единственный источник правды)
scripts/alarm_mesh.py     — mesh-будильник (крутится на каждой машине)
scripts/link_server.py    — HTTP API на порт 8080
scripts/start.sh          — запуск всех сервисов (onStart hook Firebase)
local/connect.sh          — подключение к машинам с мака
local/deploy.py           — деплой на машины
local/add_machine.py      — добавление новой машины в mesh
```

## Сценарий 1: Добавление новой машины

Пользователь пишет: "Новая тачка, вот SSH: TOKEN@lon1.tmate.io"

```bash
cd <repo>/local
python3 add_machine.py TOKEN
```

Скрипт автоматически:
1. Подключится и определит workspace, web_host, account
2. Назначит следующую букву (E, F, G...)
3. Обновит `machines.json`
4. Задеплоит alarm_mesh на новую машину
5. Обновит `machines.json` на всех существующих машинах

### Ручные шаги (нужен пользователь):
- **gcloud auth** на новой машине для ВСЕХ аккаунтов из machines.json:
  ```bash
  gcloud auth login felddaria8@gmail.com --no-launch-browser
  gcloud auth login andrewshipilovtest@gmail.com --no-launch-browser
  ```
  Пользователь должен открыть URL и вставить auth code.

- Если **новый аккаунт** (не felddaria8 и не andrewshipilovtest):
  1. Авторизовать новый аккаунт на маке: `gcloud auth login NEW@gmail.com`
  2. Авторизовать на ВСЕХ существующих машинах (через SSH на каждую)

## Сценарий 2: Передеплой после обновления кода

```bash
cd <repo>/local
python3 deploy.py ALL          # Полный деплой на все машины
python3 deploy.py A B          # Только на A и B
python3 deploy.py --config     # Только обновить machines.json
```

## Сценарий 3: Проверка статуса

```bash
cd <repo>/local
bash connect.sh status         # Проверить все машины
bash connect.sh A              # Подключиться к машине A
bash connect.sh list           # Показать все машины
```

## Сценарий 4: Восстановление после полного падения

Если все машины заснули и alarm_mesh не смог их разбудить:

1. Открыть Firebase Studio UI: `studio.firebase.google.com`
2. Зайти в каждый workspace (это пробудит машину)
3. Подождать 2-3 минуты
4. Проверить: `bash connect.sh status`
5. Если alarm_mesh не запустился автоматически (start.sh onStart):
   ```bash
   python3 deploy.py ALL
   ```

## Технические детали

### SSH через tmate
- Подключение ТОЛЬКО через `pty.fork()` + `ssh -tt TOKEN@lon1.tmate.io`
- НЕ передавать команды через `ssh ... "command"` — tmate это не поддерживает
- Отключение: `\x02d` (Ctrl-B d, tmux detach), НИКОГДА `exit`
- Файлы передаются base64+gzip чанками по 800 символов

### Python на серверах (Nix)
- `python3` нет в PATH — используется `/nix/store/*/bin/python3`
- start.sh создаёт symlink `/tmp/python3`

### Порт 8080 — cloudworkstations.dev proxy
- ВСЕ запросы к порту 8080 идут через Google proxy
- Требуется Bearer token: `gcloud auth print-access-token --account=ACCOUNT`
- URL: `https://8080-{web_host}/endpoint`

### machines.json
- Лежит в `config/machines.json` (в репо) и `/tmp/machines.json` (на каждой машине)
- alarm_mesh.py ищет в: `/tmp/machines.json` → `../config/machines.json` → рядом с собой

### gcloud аккаунты
- Каждая машина должна иметь ВСЕ аккаунты из mesh авторизованными
- Токены живут ~60 мин, refresh token ~6 мес
- alarm_mesh кеширует токены на 45 мин

### Что НЕ делать
- НЕ отправлять `exit` в tmate сессию
- НЕ создавать `~/.tmate.conf`
- НЕ использовать `tmux` команды (version mismatch)
- НЕ отправлять base64 строки >800 символов (обрезаются)
- НЕ трогать Machine A без крайней необходимости (на ней завязаны другие сервисы)
