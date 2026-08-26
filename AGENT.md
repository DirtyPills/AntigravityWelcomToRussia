# AGENT.md — инструкция для агента сопровождения

## Назначение проекта

`agy-net` изолирует Antigravity в Linux network namespace и разрешает ему сеть только через уже существующий транспорт: AmneziaVPN `amn0` или VLESS-клиент в TUN-режиме. Это не VPN-клиент, не менеджер VLESS-ссылок и не средство изменения маршрутов хоста.

## Критические инварианты

1. Никогда не создавай, не останавливай, не перенастраивай и не перемещай существующие VPN/TUN-интерфейсы, Wi-Fi или маршруты хоста.
2. Управляй только namespace `agy-net`, veth `agy-host0`/`agy-net0`, файлами `/run/agy-net` и nftables-таблицами `ip agy_net`/`inet agy_net`.
3. Не читай вслух, не записывай в репозиторий и не выводи содержимое реальных `awg0.conf`, VLESS-ссылок, private/preshared keys, токенов, cookies или журналов.
4. Не ослабляй kill switch: из `agy-host0` разрешён только выбранный транспорт; весь иной выход должен оставаться `DROP`.
5. Не запускай сетевые тесты, `start`, `stop` или `restart` без явного разрешения владельца машины: они меняют только `agy-net`, но могут закрыть приложения внутри namespace.

## Установка: AmneziaVPN

```sh
sudo ./install.sh
cp example/awg0.conf.example ./awg0.conf
chmod 600 ./awg0.conf
# Пользователь самостоятельно заполняет рабочую конфигурацию.
sudo agy-net configure ./awg0.conf
sudo agy-net doctor
sudo agy-net start
```

Перед `start` убедись только командами чтения, что `amn0` существует, включён `net.ipv4.ip_forward`, а других тоннелей и маршрутов проект не затронет.

Если AWG-конфиг нельзя переносить на машину, но `amn0` уже подключён, используй `systemd/agy-net-amnezia-dns.conf.example` как drop-in `dns.conf`. Он задаёт только DNS namespace и не читает секреты AWG.

## Установка: VLESS TUN

Допустим только заранее поднятый VLESS TUN-интерфейс. SOCKS5/HTTP proxy без интерфейса не поддерживается.

```sh
sudo env AGY_NET_TRANSPORT_INTERFACE=tun0 \
  AGY_NET_DNS=1.1.1.1,1.0.0.1 \
  agy-net doctor
sudo env AGY_NET_TRANSPORT_INTERFACE=tun0 \
  AGY_NET_DNS=1.1.1.1,1.0.0.1 \
  agy-net start
```

Заменяй `tun0` только на подтверждённое имя интерфейса. Не запускай Xray, sing-box, V2Ray и не принимай VLESS-URL как аргумент проекта.

Для systemd используй `systemd/agy-net-vless.conf.example` как drop-in `/etc/systemd/system/agy-net.service.d/vless.conf`, затем `sudo systemctl daemon-reload` и только после согласования — `sudo systemctl restart agy-net.service`.

## Запуск Antigravity

Запускать GUI нужно из активной графической сессии и от обычного пользователя:

```sh
sudo --preserve-env=DISPLAY,WAYLAND_DISPLAY,XDG_RUNTIME_DIR,DBUS_SESSION_BUS_ADDRESS,XAUTHORITY,LANG,LC_ALL \
  agy-net desktop-configure --binary /абсолютный/путь/к/antigravity-ide
```

Затем допустимы ярлык `desktop/agy-net-antigravity.desktop` или `sudo ... agy-net run /абсолютный/путь/к/antigravity-ide`. После запуска проверь, что основной PID Antigravity находится в `agy-net`, но не завершай пользовательский экземпляр без явного разрешения.

## Проверка изменений

Для изменений кода обязательно выполни:

```sh
python3 -m py_compile agy_net.py desktop_exec.py
python3 -m unittest discover -s tests -v
desktop-file-validate desktop/agy-net-antigravity.desktop
systemd-analyze verify systemd/agy-net.service systemd/agy-net@.service systemd/agy-net-antigravity@.service
```

Проверка реальной сети нужна только с разрешения владельца:

```sh
sudo agy-net test
sudo agy-net test-killswitch
```

После тестов удаляй только явно созданные `__pycache__` и временные файлы проекта, не трогая рабочую конфигурацию.

## Подготовка публикации

Перед коммитом запусти проверку без вывода секретов:

```sh
git status --short
git diff --cached --check
git ls-files
git check-ignore -v awg0.conf dns.yaml example/private.key run/agy-net.log
```

В Git разрешены только исходный код, systemd-юниты, безопасные шаблоны, тесты, документация и графика из `assets/`. Рабочие `.conf`, VLESS-ссылки, DNS-маршруты, ключи, токены, журналы, кэши и локальные файлы всегда остаются вне репозитория.
