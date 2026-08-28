# Подключение к Notion AI

> Custom MCP доступен в Notion на тарифах Business и Enterprise. Администратор workspace должен разрешить пользовательские MCP-серверы. Актуальные требования и права описаны в [официальной справке Notion](https://www.notion.com/help/mcp-connections-for-custom-agents).

## 1. Откройте Custom MCP

Перейдите в `Settings` → `Connections` → `Discover`, выберите фильтр `MCP` и нажмите `Custom MCP`.

![Раздел Connections и кнопка Custom MCP](images/01-open-custom-mcp.png)

## 2. Введите адрес сервера

Введите публичный HTTPS-адрес, созданный установщиком:

```text
https://mcp.example.com/mcp
```

Замените `mcp.example.com` своим доменом и нажмите `Connect`.

![Поле URL Custom MCP](images/02-enter-server-url.png)

## 3. Настройте Bearer Token

Укажите:

1. URL MCP-сервера.
2. Имя подключения: `Persistent VPS Compute`.
3. Authentication: `Bearer token`, затем вставьте токен в защищённое поле.

Получить чистое значение токена на VPS можно командой:

```bash
sudo /opt/mcp-compute/scripts/show-token.sh
```

Не добавляйте `MCP_TOKEN=` или `Bearer ` и никогда не вставляйте токен в Skill, README, issue или обычное сообщение.

![Настройка имени и Bearer Token](images/03-configure-bearer-token.png)

## 4. Разрешения инструмента

После подключения появится один инструмент `terminal`.

- `Always ask` — Notion просит подтверждение перед каждым запуском.
- `Run automatically` — агент запускает инструмент сам в разрешённых сценариях.
- `Always allow` — максимальное поведение в стиле Devin без постоянных подтверждений.

`Always allow` фактически выдаёт Notion Agent постоянный root-доступ к VPS. Не включайте его для агента, которым пользуются посторонние.

## 5. Добавьте Skill

Откройте `Library` → `Skills`, нажмите `New` и вставьте содержимое [`notion-skill/SKILL.md`](../notion-skill/SKILL.md) в новую страницу.

![Создание нового Skill](images/04-create-skill.png)

Рекомендуемые значения:

- Name: `Persistent VPS Compute`
- Description: `Use the connected permanent VPS to develop, test, deploy, debug, and operate software end to end through one root terminal.`
- Use automatically: включить, если агент должен сам выбирать этот Skill.

Для Custom Agent дополнительно добавьте страницу Skill в `Tools & Access`.

## Перенос между аккаунтами

Skill можно дублировать или расшарить как обычную Notion-страницу. MCP-подключение переносится повторным добавлением того же URL и авторизацией в новом аккаунте. Подключения Notion не копируются между агентами автоматически.

Если старый аккаунт больше не должен иметь доступ, после переезда отключите его и выполните:

```bash
sudo /opt/mcp-compute/scripts/rotate-token.sh
```
