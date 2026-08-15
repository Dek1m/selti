# Миграция OpenVPN с TCP на UDP — MikroTik hAP ax²

> **Устройство:** MikroTik hAP ax² (RouterOS 7.22.2, ARM64)  
> **VPN-хост:** vpn.argenta.pro:1194  
> **Дата:** Август 2026

---

## Зачем это нужно

OpenVPN по TCP работает, но создаёт проблему **TCP-over-TCP meltdown**: когда TCP-туннель OpenVPN передаёт внутри себя трафик, который тоже использует TCP (Shadowsocks, HTTPS), два стека TCP конфликтуют — происходит потеря пакетов и деградация скорости.

**Симптомы:**
- Instagram не загружается
- Telegram и YouTube работают (используют UDP-протоколы или устойчивые TCP-соединения)
- Периодические обрывы при большом объёме данных

**Решение:** Переключение OpenVPN сервера с TCP на UDP. UDP не имеет встроенной системы контроля доставки, поэтому TCP-over-UDP не конфликтует.

---

## Что делаем (кратко)

| # | Действие | Риск простоя |
|---|----------|--------------|
| 1 | Проверяем текущее состояние | Нет |
| 2 | Создаём бэкап конфигурации | Нет |
| 3 | Останавливаем старый TCP-сервер | Да (~2 мин) |
| 4 | Удаляем TCP-сервер, создаём UDP | Да (~1 мин) |
| 5 | Настраиваем Firewall (UDP 1194) | Нет |
| 6 | Обновляем конфиги клиентов | Нет |
| 7 | Проверяем работу | Нет |

**Примерный downtime:** 3–5 минут.

---

## Шаг 0. Подготовка

### 0.1 Подключаемся к роутеру

Через SSH или WinBox (10.0.0.1):

```bash
ssh dek1m@10.0.0.1
```

### 0.2 Проверяем текущее состояние

```routeros
/interface ovpn-server server print
```

Ожидаемый вывод — сервер с `protocol=tcp` и `enabled=yes`. Запоминаем текущие параметры.

### 0.3 Создаём бэкап

```routeros
/export file=backup-before-ovpn-migration
```

Файл сохранится в `/files/backup-before-ovpn-migration.rsc`. Скачайте его на компьютер через WinBox или SCP.

---

## Шаг 1. Остановка старого TCP-сервера

```routeros
/interface ovpn-server server set enabled=no
```

> ⚠️ **С этого момента все VPN-клиенты отключены.** Работа идёт через локальную сеть.

### Проверяем что сервер остановлен

```routeros
/interface ovpn-server server print
```

`running` должен быть `false`.

---

## Шаг 2. Удаление TCP-сервера и создание UDP

### 2.1 Удаляем старый сервер

```routeros
/interface ovpn-server server remove [find where name="ovpn-server1"]
```

> Если имя отличается — используйте `print` для поиска.

### 2.2 Создаём UDP-сервер

```routeros
/interface ovpn-server server add \
    name="ovpn-server1" \
    protocol=udp \
    port=1194 \
    mode=ip \
    netmask=24 \
    local-address=10.8.0.1 \
    remote-address=10.8.0.2 \
    auth=sha256 \
    cipher=aes-256-gcm \
    keepalive-timeout=30 \
    max-mtu=1500 \
    enabled=yes
```

### Параметры — что за что отвечает

| Параметр | Значение | Зачем |
|----------|----------|-------|
| `protocol=udp` | UDP вместо TCP | Решает проблему TCP-over-TCP |
| `port=1194` | Стандартный порт OpenVPN | Совместимость с клиентами |
| `mode=ip` | Туннель уровня IP (TUN) | Работает на L3, маршрутизирует весь трафик |
| `netmask=24` | Маска /24 для подсети VPN | До 254 клиентов |
| `local-address=10.8.0.1` | IP сервера в VPN-подсети | Шлюз для клиентов |
| `remote-address=10.8.0.2` | Начальный IP для клиентов | Клиент получит 10.8.0.2, следующий — 10.8.0.3 |
| `auth=sha256` | Хеш-алгоритм для HMAC | Аутентификация туннеля |
| `cipher=aes-256-gcm` | Шифрование | AES-256-GCM — современный и быстрый |
| `keepalive-timeout=30` | Таймаут keepalive (сек) | Поддержание соединения, 30 сек — стандарт |
| `max-mtu=1500` | Максимальный MTU | Стандартный Ethernet, не обрезает пакеты |

### 2.3 Проверяем

```routeros
/interface ovpn-server server print
```

Ожидаемый вывод:

```
name: ovpn-server1
protocol: udp
port: 1194
mode: ip
netmask: 24
local-address: 10.8.0.1
remote-address: 10.8.0.2
auth: sha256
cipher: aes-256-gcm
running: true
enabled: yes
```

---

## Шаг 3. Настройка Firewall

### 3.1 Открываем UDP 1194 (input)

```routeros
/ip firewall filter add \
    chain=input \
    protocol=udp \
    dst-port=1194 \
    action=accept \
    place-before=0 \
    comment="OpenVPN UDP 1194"
```

> `place-before=0` — ставим в начало цепочки, чтобы правило срабатывало раньше潜在ных блокировок.

### 3.2 Закрываем TCP 1194 (если было открыто)

```routeros
/ip firewall filter remove [find where comment~"OpenVPN" and protocol=tcp]
```

Или, если хотите оставить TCP как запасной вариант:

```routeros
/ip firewall filter set [find where dst-port=1194 and protocol=tcp] disabled=yes
```

### 3.3 Проверяем правила

```routeros
/ip firewall filter print where protocol=udp and dst-port=1194
```

---

## Шаг 4. Обновление конфигурации клиентов

### 4.1 Ключевое изменение

```
# Было (TCP):
remote vpn.argenta.pro 1194 tcp-client

# Стало (UDP):
remote vpn.argenta.pro 1194 udp-client
```

### 4.2 Полный конфиг клиента (example.ovpn)

```
client
dev tun
proto udp
remote vpn.argenta.pro 1194
resolv-retry infinite
nobind
persist-key
persist-tun
remote-cert-tls server
data-ciphers AES-256-GCM:AES-128-GCM:AES-256-CBC
keepalive 10 120

# Маршрутизация всего трафика через VPN
route 0.0.0.0 0.0.0.0 vpn_gateway

# DNS-серверы (локальные)
dhcp-option DNS 10.0.0.2
dhcp-option DNS 10.0.0.1

# Сертификаты (замените на актуальные)
<ca>
[Содержимое Argenta-CA.crt]
</ca>
<cert>
[Содержимое клиентского сертификата]
</cert>
<key>
[Содержимое приватного ключа клиента]
</key>
```

### 4.3 Android (OpenVPN Connect)

1. Удалите старое профиль
2. Импортируйте новый `.ovpn` файл
3. Или вручную: Settings → Advanced → Protocol → **UDP**
4. Адрес: `vpn.argenta.pro`, порт: `1194`

### 4.4 Windows (OpenVPN GUI / OpenVPN Connect)

1. Замените файл конфига в `C:\Program Files\OpenVPN\config\`
2. Или откройте `.ovpn` в текстовом редакторе, замените `tcp-client` на `udp-client`
3. Перезапустите OpenVPN GUI

---

## Шаг 5. Проверка после миграции

### 5.1 Проверяем что сервер запущен

```routeros
/interface ovpn-server server print
```

Убедитесь что `running: true` и `enabled: yes`.

### 5.2 Проверяем активные соединения

```routeros
/interface ovpn-server server monitor
```

Или:

```routeros
/tool/netwatch print
```

### 5.3 Подключаем клиент и проверяем

**С клиента:**

```bash
# Проверяем IP через VPN
curl ifconfig.me

# Должен показать IP вашего VPN-сервера, а не домашний

# Проверяем DNS
nslookup instagram.com

# Проверяем маршрут
tracert 8.8.8.8   # Windows
traceroute 8.8.8.8  # Linux/Mac
```

### 5.4 Проверяем Instagram

Откройте Instagram в браузере или приложении. Должен загружаться ленты и сторис.

### 5.5 Проверяем UDP-трафик на роутере

```routeros
/tool/sniffer/quick interface=ether1 protocol=udp dst-port=1194
```

Должны видеть UDP-пакеты на порту 1194.

---

## Возможные проблемы и решения

### 🔴 Клиент не подключается

**Причина:** Неправильные параметры подключения или сертификат недействителен.

**Решение:**

1. Проверьте параметры подключения на клиенте (адрес, порт, протокол UDP)
2. Убедитесь что сертификат действителен:
   ```routeros
   /certificate print where common-name=vpn.argenta.pro
   ```
   `status` должен быть `valid`, `expires-after` — в будущем.

3. Проверьте логи на роутере:
   ```routeros
   /log print where topics~"ovpn"
   ```

---

### 🔴 Клиент подключается, но трафик не идёт

**Причина:** Маршрутизация не настроена или DNS не работает.

**Решение:**

1. Проверьте маршрут на клиенте:
   ```bash
   route print     # Windows
   ip route show   # Linux
   ```

2. Должна быть запись `0.0.0.0/0 via 10.8.0.1` (или `vpn_gateway`)

3. Проверьте DNS:
   ```bash
   nslookup google.com 10.0.0.2
   ```

---

### 🔴 Instagram не работает, остальное — да

**Причина:** Instagram использует TCP-соединения, которые могут блокироваться на уровне DPI.

**Решение:** Это не проблема OpenVPN. Instagram блокируется ТСПУ/DPI на уровне провайдера. Решение — Shadowsocks или VLESS+Reality через WireGuard, который уже настроен в текущей инфраструктуре.

---

### 🔴 Сервер запущен, но `running: false`

**Причина:** Порт занят или конфликт параметров.

**Решение:**

```routeros
# Проверяем нет ли другого сервиса на порту 1194
/ip service print where port=1194

# Проверяем логи
/log print where topics~"ovpn"
```

---

### 🔴 Android OpenVPN Connect не видит UDP

**Причина:** Старая версия приложения.

**Решение:** Обновите OpenVPN Connect до последней версии (0.7.64+). В настройках профиля убедитесь что протокол установлен на **UDP**.

---

### 🔴 Keepalive срабатывает слишком часто

**Причина:** Нестабильная сеть или слишком низкий `keepalive-timeout`.

**Решение:** Увеличьте таймаут:

```routeros
/interface ovpn-server server set ovpn-server1 keepalive-timeout=60
```

---

### 🔴 Клиент подключается, но сразу отключается

**Причина:** Несовпадение параметров шифрования.

**Решение:** Убедитесь что на клиенте и сервере используются одинаковые `cipher` и `auth`:

| Параметр | Сервер (MikroTik) | Клиент (.ovpn) |
|----------|-------------------|----------------|
| cipher | aes-256-gcm | data-ciphers AES-256-GCM |
| auth | sha256 | (автоматически) |

---

## Чеклист миграции

- [ ] Бэкап конфигурации создан и скачан
- [ ] TCP-сервер остановлен и удалён
- [ ] UDP-сервер создан и запущен (`running: true`)
- [ ] Firewall: UDP 1194 разрешён, TCP 1194 закрыт/отключён
- [ ] Конфиги клиентов обновлены (`tcp-client` → `udp-client`)
- [ ] Клиент подключается по UDP
- [ ] `ifconfig.me` показывает IP VPN-сервера
- [ ] DNS-резолв работает
- [ ] Instagram загружается (если используется с Shadowsocks)
- [ ] Логи роутера чисты (`/log print where topics~"ovpn"`)

---

## Примечания

- **WireGuard** — основной туннель в текущей инфраструктуре (wg2 → vpn-p-app → Shadowsocks). OpenVPN используется как запасной вариант или для клиентов, которые не поддерживают WireGuard.
- **Shadowsocks** работает поверх WireGuard, поэтому TCP-over-TCP не возникает — это проблема только для OpenVPN по TCP.
- **MTU 1500** — оптимально для hAP ax² с AX-WiFi. Если будете использовать через WiFi с PPPoE, возможно нужно снизить до 1460.
