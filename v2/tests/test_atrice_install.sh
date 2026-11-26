#!/bin/bash
# Проверка пользовательской установки atrice: каталог без root, привязка
# каталога проекта только по явному запросу, совместимость со старым именем
# и запуск установщика из чужой директории. Настоящий Go не нужен — сборку
# изображает подставной go, который записывает вызов и создаёт файл.
#
#   bash v2/tests/test_atrice_install.sh
#
# Первым аргументом можно передать путь к проверяемому install.sh — это нужно
# для мутационных прогонов на копии.
set -u
source_root=$(cd "$(dirname "$0")/../.." && pwd -P)
script=${1:-$source_root/cli/install.sh}
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

project=$work/project
bin=$work/bin
home=$work/home
go_log=$work/go.log
mkdir -p "$project/cli" "$bin" "$home"
cp "$script" "$project/cli/install.sh"
printf 'module agent-docker\n\ngo 1.21\n' > "$project/go.mod"
# Маркер корня: установщик обязан привязывать именно каталог проекта.
mkdir -p "$project/v2"
printf '#!/bin/bash\n' > "$project/v2/manage.sh"

cat > "$bin/go" <<'EOF'
#!/bin/bash
printf 'GOTOOLCHAIN=%s pwd=%s args=%s\n' "${GOTOOLCHAIN-}" "$PWD" "$*" >> "$GO_LOG"
output=''
while test $# -gt 0; do
    if test "$1" = '-o'; then shift; output=${1-}; fi
    shift
done
test -z "$output" || { printf '#!/bin/bash\nprintf stub\\n' > "$output"; chmod +x "$output"; }
exit 0
EOF
chmod +x "$bin/go"

failures=0
label=''
check() {
    local name=${label:-$*}
    if "$@"; then
        printf 'OK     %s\n' "$name"
    else
        printf 'ПРОВАЛ (строка %s) %s\n' "${BASH_LINENO[1]}" "$name"
        printf 'Код: %s\nВывод:\n%s\n' "$status" "$output"
        failures=$((failures+1))
    fi
    label=''
}
named() { label=$1; shift; check "$@"; }
has() { grep -qF -- "$2" <<<"$1"; }
lacks() { ! has "$1" "$2"; }

config=$home/.config/agent-docker/root

# run запускает установщик из каталога проекта, run_from — из указанного.
run_from() {
    local where=$1
    shift
    : > "$go_log"
    output=$( (cd "$where" && env PATH="$bin:$PATH" HOME="$home" \
        GO_LOG="$go_log" XDG_CONFIG_HOME="$home/.config" \
        bash "$project/cli/install.sh" "$@") 2>&1 </dev/null )
    status=$?
}
run() { run_from "$project" "$@"; }

echo '=== Установка по умолчанию ==='
run
named 'установка прошла без root' test "$status" -eq 0
named 'бинарник лежит в каталоге пользователя' test -x "$home/.local/bin/atrice"
named 'путь установки назван' has "$output" "$home/.local/bin/atrice"
named 'sudo не упоминается' lacks "$output" 'sudo'
named 'сборка шла из корня проекта' has "$(cat "$go_log")" "pwd=$project"
named 'тулчейн зафиксирован' has "$(cat "$go_log")" 'GOTOOLCHAIN=local'
named 'собирался пакет cli' has "$(cat "$go_log")" './cli'
named 'без --bind привязки не появилось' test ! -e "$config"
named 'сказано, как привязать' has "$output" '--bind'

echo '=== PATH без каталога установки ==='
named 'подсказка про PATH показана' has "$output" 'не в PATH'
named 'подсказана строка для profile' has "$output" 'export PATH='

echo '=== Привязка каталога проекта ==='
run --bind
named 'установка прошла' test "$status" -eq 0
named 'файл привязки создан' test -f "$config"
named 'привязан корень проекта' test "$(cat "$config")" = "$project"

echo '=== Существующая привязка без --bind ==='
printf '%s\n' "$work/чужой-каталог" > "$config"
run
named 'установка прошла' test "$status" -eq 0
named 'привязка не перезаписана' test "$(cat "$config")" = "$work/чужой-каталог"
named 'сказано, что не тронута' has "$output" 'не тронута'
named 'подсказано, как переназначить' has "$output" '--bind'
run --bind
named 'явный --bind переназначает' test "$(cat "$config")" = "$project"

echo '=== Совместимость со старым именем ==='
run --name agd
named 'установка прошла' test "$status" -eq 0
named 'бинарник под прежним именем' test -x "$home/.local/bin/agd"
named 'новое имя осталось на месте' test -x "$home/.local/bin/atrice"

echo '=== Свой каталог установки ==='
run --prefix "$work/опт/bin"
named 'установка прошла' test "$status" -eq 0
named 'бинарник в заданном каталоге' test -x "$work/опт/bin/atrice"

echo '=== Относительный каталог установки ==='
# Относительный путь считается от каталога вызова. Раньше каталог создавался
# рядом с каталогом вызова, а бинарник уходил в корень проекта: сборка идёт с
# переходом в корень, и go build -o разрешал путь уже оттуда.
relative=относительный-bin
run_from "$work" --prefix "$relative"
named 'установка прошла' test "$status" -eq 0
named 'бинарник рядом с каталогом вызова' test -x "$work/$relative/atrice"
named 'в корне проекта ничего не создано' test ! -e "$project/$relative"
named 'назван путь, где файл и лежит' has "$output" "$work/$relative/atrice"
named 'go получил абсолютный путь' has "$(cat "$go_log")" "-o $work/$relative/atrice"

echo '=== Относительный каталог с подкаталогами ==='
run_from "$work" --prefix "вложенный/bin"
named 'установка прошла' test "$status" -eq 0
named 'бинарник во вложенном каталоге' test -x "$work/вложенный/bin/atrice"
named 'в корне проекта ничего не создано' test ! -e "$project/вложенный"

echo '=== Относительный каталог из корня проекта ==='
run_from "$project" --prefix "своё-bin"
named 'установка прошла' test "$status" -eq 0
named 'бинарник рядом с каталогом вызова' test -x "$project/своё-bin/atrice"

echo '=== Точка как каталог установки ==='
mkdir -p "$work/точка"
run_from "$work/точка" --prefix .
named 'установка прошла' test "$status" -eq 0
named 'бинарник в каталоге вызова' test -x "$work/точка/atrice"
named 'в корне проекта ничего не создано' test ! -e "$project/atrice"

echo '=== Запуск установщика из чужой директории ==='
rm -f "$config" "$home/.local/bin/atrice"
run_from "$work" --bind
named 'установка прошла' test "$status" -eq 0
named 'бинарник собран' test -x "$home/.local/bin/atrice"
named 'сборка всё равно шла из корня проекта' has "$(cat "$go_log")" "pwd=$project"
named 'привязан корень проекта, а не текущий каталог' test "$(cat "$config")" = "$project"

echo '=== Отказы ==='
run --нет-такого-флага
named 'неизвестный флаг отклонён' test "$status" -eq 2
named 'показано использование' has "$output" 'Использование'
run --name 'путь/с/чертой'
named 'имя с косой чертой отклонено' test "$status" -eq 2
run --prefix
named 'prefix без значения отклонён' test "$status" -eq 2

printf '=== Итог: неудачных проверок %s ===\n' "$failures"
test "$failures" -eq 0
