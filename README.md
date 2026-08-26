<p align="center">
  <img src="assets/antigravity-russia-banner.png" alt="АНТИГРАВИТИ на фоне российского флага" width="100%">
</p>

<h1 align="center">Обход региональных ограничений Antigravity</h1>

<p align="center">
  <strong>Изолированный запуск Antigravity через отдельный AmneziaWG, уже подключённый AmneziaVPN или VLESS TUN.</strong><br>
  Linux • systemd • network namespace • kill switch
</p>

> [!IMPORTANT]
> Проект не открывает доступ к аккаунту и не обходит правила поставщика. Он даёт Antigravity изолированный выход через уже настроенное пользователем соединение. Доступность функций и моделей определяется Google, регионом, тарифом и учётной записью. Это относится и к актуальным моделям, например Gemini 3.7 Flash, если они доступны в вашем интерфейсе Antigravity.

`agy-net` помогает запускать Antigravity в России через уже работающий VPN-транспорт, не меняя сеть всего компьютера. Приложение и все его дочерние процессы получают отдельный Linux network namespace; их трафик разрешён только через выбранный интерфейс. Поддерживаются:

- отдельный AmneziaWG-интерфейс `awg0` только внутри `agy-net`;
- активный AmneziaVPN-интерфейс `amn0` в режиме повторного использования;
- уже подключённый VLESS-клиент в режиме **TUN**, например с интерфейсом `tun0` или `vless0`;
- отдельный DNS внутри namespace, включая split DNS;
- ярлык для рабочего стола и панели.

Проект не меняет существующие VPN/TUN-интерфейсы, маршруты хоста, Wi-Fi, Xray, V2Ray или sing-box и не читает VLESS-ссылки. Режим `awg` создаёт новый `awg0` только внутри `agy-net`: для handshake используется текущий маршрут хоста к числовому endpoint’у, а иной трафик через veth блокируется. Если любой выбранный транспорт пропадает, трафик из namespace блокируется и не переходит на другую сеть.

## Как это работает

```text
Antigravity и дочерние процессы (обычный пользователь)
                    │
                    ▼
            namespace agy-net
                    │ veth
                    ▼
        режим reuse: amn0 / tun0 / vless0 ──► уже подключённый транспорт
        режим awg: awg0 ──► veth ──► маршрут хоста (только UDP handshake)

Любой другой выход из agy-net ──► DROP
```

`agy-net` создаёт только namespace `agy-net`, veth-пару `agy-host0`/`agy-net0`, собственные nftables-таблицы `agy_net` и, только в режиме `awg`, интерфейс `awg0` внутри namespace. DNS монтируется приватно для Antigravity; `/etc/resolv.conf` хоста не изменяется. При запуске приложение остаётся обычным пользовательским процессом — root используется только для создания изоляции.

## Требования

| Компонент | Требование |
| --- | --- |
| ОС | Ubuntu 22.04+, Debian 12+ или Linux Mint на их основе |
| Инициализация | systemd и права `sudo` |
| Пакеты | `iproute2`, `nftables`, `curl`, Python 3; для режима `awg` — `amneziawg-tools` |
| Транспорт | отдельный AWG-конфиг и доступный маршрут хоста к endpoint’у, либо активный `amn0`/VLESS TUN в режиме reuse |
| Сеть | включённый `net.ipv4.ip_forward=1` |
| Split DNS | `dnsmasq` — только при использовании split DNS |

Скрипт только проверяет `net.ipv4.ip_forward`; он никогда не изменяет sysctl самостоятельно.

Если проверка показывает `0`, включите forwarding явно и постоянно до запуска `agy-net`:

```sh
sudo install -Dm 0644 sysctl/90-agy-net.conf.example /etc/sysctl.d/90-agy-net.conf
sudo sysctl --system
```

## Быстрый старт: отдельный AmneziaWG внутри namespace

```sh
git clone https://github.com/DirtyPills/AntigravityWelcomToRussia.git
cd AntigravityWelcomToRussia
sudo ./install.sh
sudo install -Dm 0644 sysctl/90-agy-net.conf.example /etc/sysctl.d/90-agy-net.conf
sudo sysctl --system

cp example/awg0.conf.example ./awg0.conf
chmod 600 ./awg0.conf
# Заполните awg0.conf своими данными. Не добавляйте его в Git.

sudo agy-net configure ./awg0.conf
sudo install -Dm 0644 systemd/agy-net-awg.conf.example \
  /etc/systemd/system/agy-net.service.d/awg.conf
sudo systemctl daemon-reload
sudo systemctl start agy-net.service
sudo env AGY_NET_TUNNEL_MODE=awg agy-net doctor
```

`configure` безопасно копирует конфигурацию в `/etc/agy-net/awg0.conf` с правами `0600`. В режиме `awg` копия без строки `DNS` временно создаётся в `/run/agy-net` с правами `0600` и передаётся `awg-quick` только внутри namespace. DNS подключается отдельно и приватно для Antigravity; ключи и параметры конфигурации не попадают в журналы. Через veth разрешён только UDP к числовому endpoint’у из конфига, поэтому при отключённом `awg0` выхода через `amn0` нет.

### Повторное использование `amn0` без отдельного AWG-конфига

Если `amn0` уже подключён, но переносить его AWG-конфиг на компьютер не требуется или небезопасно, задайте только публичные DNS-серверы через systemd drop-in:

```sh
sudo install -Dm 0644 systemd/agy-net-amnezia-dns.conf.example \
  /etc/systemd/system/agy-net.service.d/dns.conf
sudo systemctl daemon-reload
sudo systemctl start agy-net.service
```

Этот вариант не читает и не создаёт AWG-конфиг, использует существующий `amn0` и задаёт DNS только для namespace `agy-net`.

## Запуск через VLESS TUN

Поддерживается только уже подключённый VLESS-клиент, который создал обычный сетевой интерфейс TUN. Режимы с одним SOCKS5/HTTP-портом не подходят: namespace нужен L3-интерфейс, например `tun0`.

1. Подключите VLESS-клиент самостоятельно и найдите его TUN-интерфейс:

   ```sh
   ip -brief link
   ```

2. Укажите интерфейс и DNS. Для разового запуска:

   ```sh
   sudo env AGY_NET_TRANSPORT_INTERFACE=tun0 \
     AGY_NET_DNS=1.1.1.1,1.0.0.1 \
     agy-net start
   ```

3. Проверьте, что изолированный трафик проходит через нужный транспорт:

   ```sh
   sudo agy-net status
   sudo agy-net test
   sudo agy-net test-killswitch
   ```

Для постоянного запуска через systemd создайте drop-in из безопасного шаблона:

```sh
sudo install -Dm 0644 systemd/agy-net-vless.conf.example \
  /etc/systemd/system/agy-net.service.d/vless.conf
sudoedit /etc/systemd/system/agy-net.service.d/vless.conf
sudo systemctl daemon-reload
sudo systemctl restart agy-net.service
```

В `vless.conf` замените `tun0` и DNS на значения вашей системы. При смене транспорта сначала остановите Antigravity и `agy-net`; это удалит только namespace и правила `agy_net`, но не сам VLESS-клиент.

> [!WARNING]
> VLESS TUN-клиенты по-разному используют policy routing. До обычной работы Antigravity обязательно выполните `sudo agy-net test` и `sudo agy-net test-killswitch`. Если тест не проходит, остановите `agy-net`: это безопасно уберёт только его namespace и правила.

## Запуск Antigravity и ярлык

Из активной графической сессии один раз задайте исполняемый файл Antigravity:

```sh
sudo --preserve-env=DISPLAY,WAYLAND_DISPLAY,XDG_RUNTIME_DIR,DBUS_SESSION_BUS_ADDRESS,XAUTHORITY,LANG,LC_ALL \
  agy-net desktop-configure --binary /абсолютный/путь/к/antigravity-ide
```

Установите ярлык для панели и рабочего стола:

```sh
install -Dm 0644 desktop/agy-net-antigravity.desktop \
  "$HOME/.local/share/applications/agy-net-antigravity.desktop"
update-desktop-database "$HOME/.local/share/applications"
```

При первом старте PolicyKit запросит обычную авторизацию. Systemd автоматически поднимет `agy-net`, а Antigravity запустится внутри namespace. Если экземпляр уже работает, второй не создаётся. Ручной запуск для диагностики:

```sh
sudo --preserve-env=DISPLAY,WAYLAND_DISPLAY,XDG_RUNTIME_DIR,DBUS_SESSION_BUS_ADDRESS,XAUTHORITY,LANG,LC_ALL \
  agy-net run /абсолютный/путь/к/antigravity-ide
```

### Linux Mint / XFCE: окно «Failed to execute default Web Browser»

На первом входе Antigravity открывает браузер для авторизации. В XFCE штатный `exo-open` иногда возвращает `Input/output error` из systemd-сервиса. Начиная с текущей версии `desktop-configure` сохраняет для ярлыка `XDG_CURRENT_DESKTOP=X-Generic` и путь к найденному Firefox/Chrome/Chromium, поэтому браузер запускается напрямую, не через `exo-open`.

После обновления проекта переустановите файлы и заново сохраните параметры ярлыка из активной графической сессии, затем перезапустите Antigravity:

```sh
sudo ./install.sh
sudo --preserve-env=DISPLAY,WAYLAND_DISPLAY,XDG_RUNTIME_DIR,DBUS_SESSION_BUS_ADDRESS,XAUTHORITY,LANG,LC_ALL \
  agy-net desktop-configure --binary /абсолютный/путь/к/antigravity-ide
sudo systemctl restart agy-net-antigravity@$USER.service
```

Это меняет только окружение процесса Antigravity; браузер, VPN, TUN-интерфейсы и маршруты хоста не перенастраиваются.

## Команды

```sh
sudo agy-net status                 # состояние namespace и транспорта
sudo agy-net doctor                 # зависимости и конфигурация
sudo agy-net dns-test api.ipify.org # DNS внутри namespace
sudo agy-net test                   # проверка HTTPS из namespace
sudo agy-net test-killswitch        # временно отключает awg0 либо default route только в namespace
sudo agy-net wg-status              # handshake управляемого awg0 в режиме awg
sudo agy-net logs                   # журнал с редактированием секретов
sudo agy-net stop                   # удаляет только agy-net и его nftables-таблицы
```

## Split DNS

В режиме AmneziaVPN DNS по умолчанию берётся из AWG-конфигурации. Чтобы включить split DNS, создайте `/etc/agy-net/dns.yaml`:

```yaml
dns:
  mode: split
  default:
    - 1.1.1.1
  routes:
    internal.example:
      - 10.10.10.53
```

`dnsmasq` запускается только внутри `agy-net` на `127.0.0.53`. Этот файл может содержать внутренние DNS-адреса, поэтому он исключён из Git.

## Простая установка с CLI-агентом

Для простой установки скормите своему CLI-агенту файл [AGENT.md](AGENT.md): он сам проверит требования, установит `agy-net`, настроит выбранный транспорт и корректный запуск Antigravity, не затрагивая существующие туннели.

## Удаление

```sh
sudo agy-net stop
sudo agy-net uninstall
# Только если нужно удалить и рабочую конфигурацию:
sudo agy-net uninstall --purge-config
```

Без `--purge-config` рабочий `/etc/agy-net/awg0.conf` сохраняется.
