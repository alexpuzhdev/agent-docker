#!/bin/bash
# Узкая идемпотентная установка канала сессий на уже созданного гостя.
#
#   bash v2/install-sessions-guest.sh --check [--root DIR] [--with-direct]
#   bash v2/install-sessions-guest.sh --install [--root DIR] [--with-tmux] [--with-direct]
#
# Ставит ровно пять файлов и больше ничего:
#   /usr/local/lib/atrice/sessions-responder.py
#   /etc/systemd/system/atrice-sessions.service
#   /etc/udev/rules.d/60-atrice-control.rules
#   /usr/lib/tmpfiles.d/atrice-sessions.conf
#   /etc/profile.d/atrice-tmux.sh
# С --with-direct добавляются ещё четыре файла для прямого входа —
# прямого входа в сессию мимо serial-консоли:
#   /usr/local/lib/atrice/session-responder.py
#   /etc/systemd/system/atrice-session.service
#   /etc/systemd/system/atrice-tmux-server.service
#   /etc/udev/rules.d/60-atrice-session.rules
# Без флага она не ставится и не включается: канал списка остаётся читающим, а
# прямой вход — явно включаемым механизмом. Порт atrice.session появляется при
# следующем запуске гостя, поэтому до перезапуска служба просто ждёт устройства.
# Последние два задают общий сокет tmux: каталог сокета создаётся заранее, и
# ни агент, ни служба его не создают — служба при ProtectSystem=strict и не
# может. Без этого первый запрос до первого запуска tmux отвечал отказом.
# Ключи хоста, учётные данные, профиль VM, /etc/hosts, домашние каталоги и
# любые чужие файлы не читаются и не перезаписываются. Повторный запуск при
# совпадающем содержимом не пишет ничего и ничего не перезапускает.
#
# Новым гостям скрипт не нужен: те же файлы доставляет cloud-init при первом
# запуске. Он существует для гостей, созданных до появления канала, потому что
# cloud-init применяет write_files один раз на instance-id, а менять
# instance-id нельзя: на копии старого диска это вызвало генерацию нового
# ключа хоста ED25519 и смену его отпечатка.
#
# Запускает оператор, одним из двух способов:
#   1. На остановленном госте, смонтировав его корень на хосте:
#        bash v2/install-sessions-guest.sh --install --root /mnt/guest
#      Права root внутри гостя не нужны. Юнит включается здесь же: создаётся
#      та же ссылка в multi-user.target.wants, что делает systemctl enable,
#      поэтому systemctl --root=DIR is-enabled отвечает enabled, а служба на
#      хосте не запускается. Это предпочтительный способ.
#   2. Внутри работающего гостя, от root:
#        bash install-sessions-guest.sh --install
#      Тогда правило udev и служба применяются сразу.
# Агенту новых прав не выдаётся, сеть не используется.
#
# Корень гостевого диска недоверенный. Все целевые пути проверяются до любых
# изменений: символическая ссылка в родительском каталоге, в самом целевом
# файле или в пути включения отклоняется, и запись за пределы корня
# невозможна. Исключение одно — сама ссылка включения, и её значение
# сверяется с ожидаемым.
set -euo pipefail

source_root=$(cd "$(dirname "$0")/.." && pwd -P)
sources_list=${AGENT_DOCKER_APT_LIST:-/etc/apt/agent-docker.list}
mode=''
prefix=''
root_given=''
with_tmux=''
with_direct=''
usage() {
    printf 'Использование: %s --check|--install [--root DIR] [--with-tmux] [--with-direct]\n' \
        "${0##*/}" >&2
    exit 2
}
while test $# -gt 0; do
    case $1 in
        --check|--install) mode=${1#--} ;;
        --root) shift; test $# -gt 0 || usage; prefix=$1; root_given=1 ;;
        --root=*) prefix=${1#--root=}; root_given=1 ;;
        --with-tmux) with_tmux=1 ;;
        --with-direct) with_direct=1 ;;
        *) usage ;;
    esac
    shift
done
test -n "$mode" || usage

unit_name=atrice-sessions.service
unit_path=/etc/systemd/system/$unit_name
wants_link=/etc/systemd/system/multi-user.target.wants/$unit_name
# Служб может быть две: канал списка и, по явному флагу, прямой вход.
units=("$unit_name")
test -z "$with_direct" || units+=(atrice-session.service)
unit_path_of() { printf '/etc/systemd/system/%s' "$1"; }
wants_link_of() { printf '/etc/systemd/system/multi-user.target.wants/%s' "$1"; }
root_real=''
# Пустое значение --root нельзя молча считать режимом внутри гостя: оператор
# просил офлайн-установку, и подменять её записью в хостовую систему нельзя.
if test -n "$root_given" && test -z "$prefix"; then
    echo '--root не может быть пустым.' >&2
    exit 2
fi
if test -n "$prefix"; then
    # Офлайн-режиму нужен явный отдельный корень: пустое значение и / означали
    # бы запись в хостовую систему.
    test "$prefix" != '/' ||
        { echo 'Офлайн-режиму нужен корень гостя, а не /.' >&2; exit 2; }
    prefix=${prefix%/}
    test -n "$prefix" || { echo '--root не может быть пустым.' >&2; exit 2; }
    test -d "$prefix" || { printf 'Каталог %s не существует.\n' "$prefix" >&2; exit 2; }
    root_real=$(realpath -- "$prefix") ||
        { printf 'Не удалось разрешить путь %s.\n' "$prefix" >&2; exit 2; }
    test "$root_real" != '/' ||
        { echo 'Офлайн-режиму нужен корень гостя, а не /.' >&2; exit 2; }
fi

# Источник | цель внутри гостя | права.
payload=(
    "$source_root/v2/sessions-responder.py|/usr/local/lib/atrice/sessions-responder.py|0755"
    "$source_root/v2/atrice-sessions.service|/etc/systemd/system/atrice-sessions.service|0644"
    "$source_root/v2/atrice-control.rules|/etc/udev/rules.d/60-atrice-control.rules|0644"
    "$source_root/v2/atrice-sessions.tmpfiles.conf|/usr/lib/tmpfiles.d/atrice-sessions.conf|0644"
    "$source_root/v2/atrice-tmux-profile.sh|/etc/profile.d/atrice-tmux.sh|0644"
)
if test -n "$with_direct"; then
    payload+=(
        "$source_root/v2/session-responder.py|/usr/local/lib/atrice/session-responder.py|0755"
        "$source_root/v2/atrice-session.service|/etc/systemd/system/atrice-session.service|0644"
        "$source_root/v2/atrice-tmux-server.service|/etc/systemd/system/atrice-tmux-server.service|0644"
        "$source_root/v2/atrice-session.rules|/etc/udev/rules.d/60-atrice-session.rules|0644"
    )
fi
tmpfiles_config=/usr/lib/tmpfiles.d/atrice-sessions.conf

refuse() {
    printf 'Отказ: %s — %s\n' "$1" "$2" >&2
    exit 3
}

# guard_path проверяет один целевой путь: ни один существующий компонент не
# должен быть символической ссылкой, а сам путь — выходить за корень. Проверка
# выполняется до любых изменений, поэтому опасный корень не приводит к записи.
guard_path() {
    local target=$1 allow_link=${2:-} current=$prefix component resolved check_path
    local -a parts
    IFS='/' read -r -a parts <<<"${target#/}"
    for component in "${parts[@]}"; do
        current=$current/$component
        if test -L "$current"; then
            if test -n "$allow_link" && test "$current" = "$prefix$target"; then
                break
            fi
            refuse "$current" 'символическая ссылка в целевом пути'
        fi
        test -e "$current" || break
    done
    # Защита от выхода за корень. Саму ссылку включения разыменовывать нельзя:
    # её цель — абсолютный путь внутри гостя, и на хосте он ведёт за корень по
    # построению. Для неё проверяется родительский каталог, а значение ссылки
    # сверяется отдельно в guard_targets.
    if test -n "$root_real"; then
        check_path=$prefix$target
        if test -n "$allow_link" && test -L "$check_path"; then
            check_path=$(dirname -- "$check_path")
        fi
        resolved=$(realpath -m -- "$check_path") ||
            refuse "$check_path" 'путь не разрешается'
        case $resolved in
            "$root_real"|"$root_real"/*) ;;
            *) refuse "$check_path" 'путь ведёт за пределы корня' ;;
        esac
    fi
}

# guard_targets проверяет всё, к чему установщик может прикоснуться.
guard_targets() {
    local entry target
    for entry in "${payload[@]}"; do
        IFS='|' read -r _ target _ <<<"$entry"
        guard_path "$target"
        if test -e "$prefix$target" && ! test -f "$prefix$target"; then
            refuse "$prefix$target" 'цель существует и не является обычным файлом'
        fi
    done
    local unit link
    guard_path "$(dirname "$wants_link")"
    for unit in "${units[@]}"; do
        link=$(wants_link_of "$unit")
        guard_path "$link" allow-link
        if test -e "$prefix$link" || test -L "$prefix$link"; then
            test -L "$prefix$link" ||
                refuse "$prefix$link" 'путь включения существует и не является ссылкой'
            test "$(readlink -- "$prefix$link")" = "$(unit_path_of "$unit")" ||
                refuse "$prefix$link" 'ссылка включения ведёт не на юнит гостя'
        fi
    done
}

# Границы проверяются до всего остального: даже --check не должен читать
# сквозь подложенную ссылку.
guard_targets

enabled_offline() {
    local link
    link=$(wants_link_of "$1")
    test -L "$prefix$link" &&
        test "$(readlink -- "$prefix$link")" = "$(unit_path_of "$1")"
}

state=0
missing=0
for entry in "${payload[@]}"; do
    IFS='|' read -r origin target _ <<<"$entry"
    test -r "$origin" || { printf 'Нет исходного файла %s\n' "$origin" >&2; exit 2; }
    if ! test -e "$prefix$target"; then
        printf 'нет        %s\n' "$target"
        missing=1
    elif cmp -s "$origin" "$prefix$target"; then
        printf 'на месте   %s\n' "$target"
    else
        printf 'отличается %s\n' "$target"
        missing=1
    fi
done

if test "$mode" = 'check'; then
    for unit in "${units[@]}"; do
        if test -n "$prefix"; then
            if enabled_offline "$unit"; then
                printf 'включён    %s\n' "$(wants_link_of "$unit")"
            else
                printf 'не включён %s\n' "$(wants_link_of "$unit")"
                missing=1
            fi
        elif systemctl is-enabled --quiet "$unit" 2>/dev/null; then
            printf 'включён    %s\n' "$unit"
        else
            printf 'не включён %s\n' "$unit"
            missing=1
        fi
    done
    if test -z "$prefix" && ! command -v tmux >/dev/null; then
        echo 'нет        tmux: живые сессии показывать нечем'
        missing=1
    fi
    # Каталог сокета проверяется отдельно: без него служба отвечает пустым
    # списком даже при живой сессии, и это та самая неисправность, из-за
    # которой ответ зависел от первого запуска tmux.
    if test -z "$prefix"; then
        socket_dir=$(awk '$1 == "d" && $2 ~ /\/sessions$/ { print $2 }' \
            "$source_root/v2/atrice-sessions.tmpfiles.conf" | tail -1)
        if test -n "$socket_dir" && test -d "$socket_dir"; then
            printf 'на месте   %s\n' "$socket_dir"
        else
            printf 'нет        %s: каталог сокета tmux\n' "${socket_dir:-каталог сокета}"
            missing=1
        fi
    fi
    test "$missing" -eq 0 || { echo 'Канал сессий не установлен полностью.'; exit 1; }
    echo 'Канал сессий установлен.'
    exit 0
fi

if test -z "$prefix"; then
    test "$(id -u)" -eq 0 ||
        { echo 'Установка внутри гостя требует root. Либо укажите --root с путём к корню гостя.' >&2
          exit 1; }
fi

changed=0
for entry in "${payload[@]}"; do
    IFS='|' read -r origin target mode_bits <<<"$entry"
    destination=$prefix$target
    if test -e "$destination" && cmp -s "$origin" "$destination"; then
        continue
    fi
    mkdir -p "$(dirname "$destination")"
    # Запись через временный файл рядом с целью: прерванная установка не
    # оставляет обрезанного файла на месте рабочего.
    draft=$(mktemp "$(dirname "$destination")/.atrice.XXXXXX")
    cat "$origin" > "$draft"
    chmod "$mode_bits" "$draft"
    mv -- "$draft" "$destination"
    printf 'записан    %s\n' "$target"
    changed=1
done

if test "$changed" -eq 0; then
    echo 'Всё уже на месте: ничего не записано.'
else
    printf 'Записано файлов: изменения внесены только в перечисленные пути.\n'
fi

if test -n "$prefix"; then
    # Включение без systemd на хосте: создаётся та же ссылка, что делает
    # systemctl enable, с путём юнита внутри гостя. Повторный запуск при
    # верной ссылке ничего не меняет.
    for unit in "${units[@]}"; do
        link=$(wants_link_of "$unit")
        if enabled_offline "$unit"; then
            printf 'уже включён %s\n' "$link"
            continue
        fi
        mkdir -p "$prefix$(dirname "$link")"
        link_draft=$(mktemp -u "$prefix$(dirname "$link")/.atrice.XXXXXX")
        ln -s -- "$(unit_path_of "$unit")" "$link_draft"
        mv -T -- "$link_draft" "$prefix$link"
        printf 'включён    %s\n' "$link"
        changed=1
    done
    echo 'Корень смонтирован: служба поднимется при следующем запуске гостя.'
    exit 0
fi

if test -n "$with_tmux" && ! command -v tmux >/dev/null; then
    test -r "$sources_list" ||
        { printf 'Нет списка источников %s: другие репозитории не добавляются.\n' \
            "$sources_list" >&2; exit 1; }
    apt_options=(
        -o Dir::Etc::sourcelist="$sources_list"
        -o Dir::Etc::sourceparts=-
        -o Acquire::Retries=0
    )
    apt-get "${apt_options[@]}" update
    apt-get "${apt_options[@]}" install -y --no-install-recommends tmux
fi

if test "$changed" -ne 0; then
    udevadm control --reload
    udevadm trigger --subsystem-match=virtio-ports
    systemctl daemon-reload
fi
# Каталог сокета создаётся до запуска службы: при загрузке это делает
# systemd-tmpfiles сам, здесь файл применяется вручную. Вызов идемпотентен —
# существующий каталог только приводится к заданным правам, содержимое не
# трогается, живой сокет агента остаётся на месте.
systemd-tmpfiles --create "$tmpfiles_config"
for unit in "${units[@]}"; do
    if test "$unit" = atrice-session.service && systemctl is-active --quiet "$unit" &&
        { test "$changed" -ne 0 || ! systemctl is-active --quiet atrice-tmux-server.service; }; then
        systemctl enable "$unit"
        printf '%s: обновление вступит в силу при следующем запуске гостя.\n' "$unit"
        continue
    fi
    systemctl enable --now "$unit"
    systemctl is-active --quiet "$unit" && printf 'Служба %s активна.\n' "$unit"
done
exit "$state"
