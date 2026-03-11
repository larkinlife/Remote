# Firebase Alarm — перекрёстный будильник для Firebase Studio

Система автоматического пробуждения Firebase Studio workspace'ов по расписанию.

## Быстрый старт

```bash
# Создать workspace из этого репо:
# https://studio.firebase.google.com → Import repo → https://github.com/larkinlife/Remote

# Получить SSH-ссылку (после запуска ~30с):
curl https://8080-<workspace-id>.cloudworkstations.dev/links

# Подключиться:
ssh -tt -o "SetEnv TERM=xterm-256color" -o "ServerAliveInterval=15" -o "ServerAliveCountMax=3" <TOKEN>@lon1.tmate.io
```

## Как это работает

Три Firebase Studio workspace'а будят друг друга по кругу:

```
 ┌──────────┐     curl      ┌──────────┐
 │ Machine A │──────────────▶│ Machine B │
 └──────────┘               └──────────┘
       ▲                          │
       │         curl             │ curl
       │                          ▼
       │                    ┌──────────┐
       └────────────────────│ Machine C │
                            └──────────┘
```

Каждая машина по cron:
1. Получает OAuth-токен через `gcloud auth print-access-token`
2. Генерирует workstation access token для целевой машины через `generateAccessToken` API
3. Стучит по `cloudworkstations.dev` URL — это будит спящий workspace

## Что разворачивается при старте

| Сервис | Порт | Описание |
|--------|------|----------|
| tmate | — | SSH-доступ через tmate.io |
| link-server | 8080 | HTTP-сервер, отдаёт tmate-ссылки |
| watchdog | — | Перезапускает упавшие сервисы каждые 60с |

## Первоначальная настройка

После создания workspace:

```bash
# 1. Авторизовать gcloud (одноразово)
gcloud auth login --no-launch-browser
# Открыть URL в браузере, вставить код

# 2. Скрипт настройки создаст всё остальное автоматически
# (wake-up cron, конфиги целевых машин)
```

## Безопасность

- SSH-ключи для git — deploy key без passphrase (write access только к этому репо)
- gcloud refresh token живёт ~6 месяцев для Gmail-аккаунтов
- tmate-ссылки ротируются при перезапуске
- OAuth-токены обновляются автоматически через gcloud
