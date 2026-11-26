#!/bin/bash
# Цели консоли в manage.sh: настоящая точка входа и подставной docker.
#
# Проверяется то, что ломалось на живом стенде:
#   - токен консоли берётся без конвейера. Привычное tr -dc ... | head -c 32
#     под set -euo pipefail обрывало tr сигналом SIGPIPE, цель заканчивалась
#     кодом 141 ещё до docker exec, и консоль не открывалась вовсе;
#   - docker exec действительно запускается, а освобождение консоли по ловушке
#     приходит с тем же токеном, что был выдан входу;
#   - vm-session-open подключается только к работающей ВМ: ни compose up, ни
#     запуска нового агента на этом пути нет, а RESUME доходит без изменений.
#
# Настоящие контейнеры, ВМ и QEMU не нужны: docker подставной, он лишь пишет
# журнал вызовов. Дерево проекта копируется в каталог теста, поэтому .env и
# состояние гостя рабочему каталогу не достаются.
set -uo pipefail

source_root=$(cd "$(dirname "$0")/.." && pwd -P)/..
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
failures=0

ok() { printf 'OK     %s\n' "$1"; }
bad() { printf 'ОШИБКА %s\n' "$1"; failures=$((failures + 1)); }
check() { if test "$2" = "$3"; then ok "$1"; else bad "$1: получено «$2», ожидалось «$3»"; fi; }
contains() {
    if grep -qF -- "$2" "$3"; then ok "$1"; else bad "$1: в $3 нет «$2»"; fi
}
missing() {
    if grep -qF -- "$2" "$3"; then bad "$1: в $3 нашлось «$2»"; else ok "$1"; fi
}

tar -C "$source_root" -cf - v2 Makefile .env.example | tar -C "$work" -xf -
mkdir -p "$work/bin" "$work/state/vm-02"
printf '2 4096 12\n' > "$work/state/vm-02/resources"
printf 'http://10.215.2.2:8888\n' > "$work/state/vm-02/upstream"

# Подставной docker: отвечает как исправный и записывает каждый вызов.
# Состояние гостя задаётся переменной FAKE_VM_RUNNING.
cat > "$work/bin/docker" <<'STUB'
#!/bin/bash
printf '%s\n' "docker $*" >> "$DOCKER_LOG"
case "$1" in
    info) printf '27.0.0\n'; exit 0 ;;
    image) exit 0 ;;
esac
if test "$1" = compose; then
    shift
    for argument in "$@"; do
        case "$argument" in
            version) exit 0 ;;
            ps) if test "${FAKE_VM_RUNNING:-0}" = 1; then printf 'c0ffee1234\n'; fi; exit 0 ;;
            up|exec) exit 0 ;;
        esac
    done
fi
exit 0
STUB
chmod +x "$work/bin/docker"

# released_with проверяет, что освобождение консоли пришло с этим токеном.
# Пустой аргумент команды в журнале вызовов не виден, поэтому строка ищется по
# признаку --release и самому токену, а не по точному расположению пробелов.
released_with() {
    test -n "$1" || return 1
    grep -F -- '--release' "$work/docker.log" | grep -qF -- "$1"
}

# target запускает цель manage.sh и оставляет журнал вызовов docker.
target() {
    local name=$1 running=$2
    : > "$work/docker.log"
    ( cd "$work" && PATH="$work/bin:$PATH" DOCKER_LOG="$work/docker.log" \
        FAKE_VM_RUNNING="$running" V2_STATE_DIR="$work/state" VM=vm-02 \
        RESUME="${RESUME:-}" bash v2/manage.sh "$name" ) > "$work/out.log" 2>&1
    printf '%s' $?
}

printf '== vm-console на работающей ВМ ==\n'
code=$(target vm-console 1)
check 'цель завершилась успешно' "$code" 0
contains 'запущен docker exec с console.py' 'console.py vm-console' "$work/docker.log"
given=$(grep -o 'AGENT_DOCKER_CONSOLE_TOKEN=[0-9a-f]*' "$work/docker.log" | head -1 | cut -d= -f2)
check 'токен длиной 32' "${#given}" 32
if released_with "$given"; then
    ok 'консоль освобождается тем же токеном'
else
    bad "освобождение не пришло с токеном «$given»"
fi

printf '== vm-session-open на остановленной ВМ ==\n'
RESUME=20261123T0915-9f3c
code=$(RESUME=$RESUME target vm-session-open 0)
check 'цель отказала' "$code" 1
contains 'отказ назвал причину' 'живых сессий в ней нет' "$work/out.log"
missing 'ВМ не поднималась' ' up ' "$work/docker.log"
missing 'ничего не запускалось в госте' 'console.py' "$work/docker.log"

printf '== vm-session-open на работающей ВМ ==\n'
code=$(RESUME=$RESUME target vm-session-open 1)
check 'цель завершилась успешно' "$code" 0
contains 'запущен attach к выбранной сессии' "console.py vm-session-open  $RESUME" "$work/docker.log"
missing 'ВМ не поднималась' ' up ' "$work/docker.log"
missing 'новый агент не запускался' 'console.py claude' "$work/docker.log"
given=$(grep -o 'AGENT_DOCKER_CONSOLE_TOKEN=[0-9a-f]*' "$work/docker.log" | head -1 | cut -d= -f2)
if released_with "$given"; then
    ok 'консоль освобождается тем же токеном'
else
    bad "освобождение не пришло с токеном «$given»"
fi

printf '== vm-session-claude на работающей ВМ ==\n'
code=$(RESUME= target vm-session-claude 1)
check 'цель завершилась успешно' "$code" 0
contains 'запущено создание новой сессии' 'console.py vm-session-claude' "$work/docker.log"
missing 'ВМ не поднималась' ' up ' "$work/docker.log"
missing 'прежний агент не запускался' 'console.py claude' "$work/docker.log"
missing 'прежняя сессия не открывалась' 'vm-session-open' "$work/docker.log"
name=$(grep -o 'console.py vm-session-claude *claude-[0-9a-f]*' "$work/docker.log" |
    head -1 | grep -o 'claude-[0-9a-f]*')
check 'имя новой сессии из 16 шестнадцатеричных знаков' "${#name}" 23
contains 'имя названо пользователю' "Новая сессия: $name" "$work/out.log"
given=$(grep -o 'AGENT_DOCKER_CONSOLE_TOKEN=[0-9a-f]*' "$work/docker.log" | head -1 | cut -d= -f2)
if released_with "$given"; then
    ok 'консоль освобождается тем же токеном'
else
    bad "освобождение не пришло с токеном «$given»"
fi
# Второй запуск обязан дать другое имя: повторное нажатие не должно
# натыкаться на уже занятое имя сессии.
first=$name
code=$(RESUME= target vm-session-claude 1)
check 'повторный запуск удался' "$code" 0
second=$(grep -o 'console.py vm-session-claude *claude-[0-9a-f]*' "$work/docker.log" |
    head -1 | grep -o 'claude-[0-9a-f]*')
if test -n "$second" && test "$second" != "$first"; then
    ok 'имя новой сессии каждый раз другое'
else
    bad "имя повторилось: «$second»"
fi

printf '== vm-session-claude на остановленной ВМ ==\n'
code=$(RESUME= target vm-session-claude 0)
check 'цель отказала' "$code" 1
contains 'отказ назвал причину' 'остановлена' "$work/out.log"
missing 'ВМ не поднималась' ' up ' "$work/docker.log"
missing 'ничего не запускалось в госте' 'console.py' "$work/docker.log"

printf '== vm-session-new: прямой путь, serial не затрагивается ==\n'
code=$(RESUME= target vm-session-new 1)
check 'цель завершилась успешно' "$code" 0
contains 'запущен прямой вход' 'session.py open' "$work/docker.log"
missing 'ВМ не поднималась' ' up ' "$work/docker.log"
missing 'консоль не открывалась' 'console.py' "$work/docker.log"
missing 'токен консоли не выдавался' 'AGENT_DOCKER_CONSOLE_TOKEN' "$work/docker.log"
name=$(grep -o 'session.py open  *claude-[0-9a-f]*' "$work/docker.log" | head -1 |
    grep -o 'claude-[0-9a-f]*')
check 'имя новой сессии из 16 шестнадцатеричных знаков' "${#name}" 23
contains 'имя названо пользователю' "Новая сессия: $name" "$work/out.log"
first=$name
code=$(RESUME= target vm-session-new 1)
second=$(grep -o 'session.py open  *claude-[0-9a-f]*' "$work/docker.log" | head -1 |
    grep -o 'claude-[0-9a-f]*')
if test -n "$second" && test "$second" != "$first"; then
    ok 'имя новой сессии каждый раз другое'
else
    bad "имя повторилось: «$second»"
fi
code=$(RESUME= target vm-session-new 0)
check 'на остановленной ВМ цель отказала' "$code" 1
missing 'ничего не запускалось в госте' 'session.py' "$work/docker.log"

printf '== vm-session-direct: вход по точному имени ==\n'
code=$(RESUME=claude-aaaabbbbccccdddd target vm-session-direct 1)
check 'цель завершилась успешно' "$code" 0
# Пустой аргумент проекта в журнале подставного docker не виден, поэтому
# совпадение идёт по операции и точному имени на одной строке.
if grep -q 'session.py open  *claude-aaaabbbbccccdddd$' "$work/docker.log"; then
    ok 'имя дошло без изменений'
else
    bad 'точное имя сессии не дошло до прямого входа'
fi
missing 'ВМ не поднималась' ' up ' "$work/docker.log"
missing 'консоль не открывалась' 'console.py' "$work/docker.log"
missing 'нового агента не запускалось' 'claude' "$work/out.log"
code=$(RESUME= target vm-session-direct 1)
check 'без RESUME цель отказала' "$code" 1
contains 'отказ просит указать сессию' 'Укажите сессию' "$work/out.log"
missing 'ничего не запускалось' 'session.py' "$work/docker.log"

printf '== vm-session-direct: токен входа и освобождение по нему ==\n'
code=$(RESUME=claude-aaaabbbbccccdddd target vm-session-direct 1)
check 'цель завершилась успешно' "$code" 0
given=$(grep -o 'AGENT_DOCKER_SESSION_TOKEN=[0-9a-f]*' "$work/docker.log" | head -1 | cut -d= -f2)
check 'токен входа длиной 32' "${#given}" 32
if grep -F -- '--release' "$work/docker.log" | grep -qF -- "$given"; then
    ok 'реле освобождается тем же токеном'
else
    bad "освобождение не пришло с токеном «$given»"
fi
contains 'освобождение идёт через session.py' 'session.py --release' "$work/docker.log"
missing 'консоль не затрагивалась' 'console.py' "$work/docker.log"

printf '== vm-session-direct-check: читающая проверка пути ==\n'
code=$(RESUME= target vm-session-direct-check 1)
check 'цель завершилась успешно' "$code" 0
contains 'запущена проверка пути' 'session.py probe' "$work/docker.log"
missing 'ВМ не поднималась' ' up ' "$work/docker.log"
missing 'консоль не открывалась' 'console.py' "$work/docker.log"
code=$(RESUME= target vm-session-direct-check 0)
check 'на остановленной ВМ цель отказала' "$code" 1

printf '== vm-shell-check: читающая диагностика оболочки ==\n'
code=$(RESUME= target vm-shell-check 1)
check 'цель завершилась успешно' "$code" 0
contains 'запущена читающая диагностика' "console.py --shell" "$work/docker.log"
missing 'ВМ не поднималась' ' up ' "$work/docker.log"
missing 'консоль не открывалась' 'AGENT_DOCKER_CONSOLE_TOKEN' "$work/docker.log"
missing 'освобождение не вызывалось' '--release' "$work/docker.log"
code=$(RESUME= target vm-shell-check 0)
check 'на остановленной ВМ цель отказала' "$code" 1
missing 'ничего не запускалось в госте' 'console.py' "$work/docker.log"

printf '== vm-session-open без RESUME ==\n'
code=$(RESUME= target vm-session-open 1)
check 'цель отказала' "$code" 1
contains 'отказ просит указать сессию' 'Укажите живую сессию' "$work/out.log"
missing 'ничего не запускалось' 'console.py' "$work/docker.log"

printf '=== Итог: неудачных проверок %d ===\n' "$failures"
test "$failures" -eq 0
