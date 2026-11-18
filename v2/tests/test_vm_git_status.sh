#!/bin/bash
# Проверка make vm-git-status с подставным docker, временным состоянием и фиктивным токеном.
set -euo pipefail
source_root=$(cd "$(dirname "$0")/../.." && pwd -P)
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
unset REPO GIT_WRITE GIT_WRITE_BRANCH VM
command -v node >/dev/null || { echo 'Для теста нужен node.' >&2; exit 1; }

mkdir -p "$work/project" "$work/bin" "$work/state/vm-02"
cp -R "$source_root/Makefile" "$source_root/.env.example" "$source_root/v2" "$work/project/"
config=$work/state/vm-02/git/repository.json
cat > "$work/bin/docker" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
if [[ -n ${FAKE_DOCKER_RUN_FAIL:-} && $1 = run ]]; then exit 125; fi
case " $* " in
    *' --settings /repository.json '*) node "$work/project/v2/git-gateway.cjs" --settings "$FAKE_GIT_CONFIG" ;;
esac
EOF
chmod +x "$work/bin/docker"
export work PATH="$work/bin:$PATH" V2_STATE_DIR="$work/state" FAKE_GIT_CONFIG="$config" FAKE_DOCKER_LOG="$work/docker.log"

target() {
    if command -v make >/dev/null; then
        make -s -C "$work/project" "$@"
    else
        (cd "$work/project" && env "${@:2}" bash v2/manage.sh "$1")
    fi
}
run_status() {
    : > "$FAKE_DOCKER_LOG"
    set +e
    output=$(target vm-git-status VM=vm-02 2>&1 </dev/null)
    status=$?
    set -e
}
check() { "$@" || { printf 'Провал (строка %s): %s\nКод: %s\nВывод:\n%s\n' "${BASH_LINENO[0]}" "$*" "$status" "$output" >&2; exit 1; }; }
has() { grep -qF -- "$1" <<<"$output"; }
lacks() { ! has "$1"; }
read_only() {
    local line
    while IFS= read -r line; do
        [[ $line == 'info --format {{.ServerVersion}}' || $line == 'compose version' ||
            $line == "run --rm --pull never --network none --mount type=bind,src=$work/project/v2,dst=/scripts,readonly --mount type=bind,src=$config,dst=/repository.json,readonly node:22-bookworm-slim node /scripts/git-gateway.cjs --settings /repository.json" ]] ||
            return 1
    done < "$FAKE_DOCKER_LOG"
}
snapshot() { (cd "$work" && find state project/.env -printf '%p %s %T@ %m\n' | sort) > "$1"; }
write_config() {
    mkdir -p "$work/state/vm-02/git"
    printf '%s\n' "$1" > "$config"
    touch -d '2001-01-01 00:00:00' "$config"
    cp -p "$config" "$work/saved.json"
}
unchanged() { cmp -s "$config" "$work/saved.json" && test "$(stat -c %Y "$config")" = "$(stat -c %Y "$work/saved.json")"; }
token=fixture_status_token_0003

run_status
check test "$status" -eq 0
check has 'Git-шлюз vm-02 не настроен.'
check has 'make vm-git VM=vm-02 REPO=owner/name'
check test ! -e "$work/state/vm-02/git"
check test -z "$(grep -F -- '--settings' "$FAKE_DOCKER_LOG")"
check read_only

write_config "{\"repository\":\"owner/repository\",\"token\":\"$token\",\"write\":false,\"writeBranch\":\"\"}"
run_status
check test "$status" -eq 0
check has 'Репозиторий: owner/repository'
check has 'Провайдер: GitHub'
check has 'Режим: только чтение'
check lacks 'запись включена'
check has 'Разрешённая ветка: нет'
check lacks "$token"
check test -n "$(grep -F -- '--settings' "$FAKE_DOCKER_LOG")"
check read_only
check unchanged
snapshot "$work/before"
run_status
check test "$status" -eq 0
snapshot "$work/after"
check cmp -s "$work/before" "$work/after"

write_config "{\"repository\":\"owner/repository\",\"token\":\"$token\",\"write\":true,\"writeBranch\":\"test/gateway-write\"}"
run_status
check test "$status" -eq 0
check has 'Режим: чтение и запись'
check has 'Разрешённая ветка: test/gateway-write'
check test "$(grep -c "$token" <<<"$output")" -eq 0
check read_only
check unchanged

write_config "{\"repository\":\"owner/repository\",\"token\":\"$token\",\"write\":true,\"writeBranch\":\"main\"}"
run_status
check test "$status" -eq 0
check has 'запись включена без допустимой ветки'
check lacks 'чтение и запись'
check has 'Разрешённая ветка: нет'
check lacks "$token"
check read_only
check unchanged

: > "$FAKE_DOCKER_LOG"
set +e
output=$(FAKE_DOCKER_RUN_FAIL=1 target vm-git-status VM=vm-02 2>&1 </dev/null)
status=$?
set -e
check test "$status" -ne 0
check has 'Docker не запустил парсер'
check lacks 'Задайте её заново'
check lacks "$token"
check unchanged

for broken in \
    "{\"repository\":\"owner/repository\",\"token\":\"$token\",\"write\":" \
    "{\"repository\":\"owner/repository.git\",\"token\":\"$token\",\"write\":false,\"writeBranch\":\"\"}" \
    "{\"repository\":\"owner/repository\",\"token\":\"$token\\\"\",\"write\":false,\"writeBranch\":\"\"}" \
    "{\"repository\":\"owner/repository\",\"token\":\"$token\",\"write\":\"yes\",\"writeBranch\":\"\"}"; do
    write_config "$broken"
    run_status
    check test "$status" -ne 0
    check has 'Не удалось прочитать сохранённую настройку Git-шлюза vm-02.'
    check test "$(grep -c "$token" <<<"$output")" -eq 0
    check read_only
    check unchanged
done

write_config "{\"provider\":\"gitlab\",\"origin\":\"https://gitlab.fixture.invalid/scm\",\"project\":\"group/sub/project\",\"token\":\"$token\",\"write\":true,\"writeBranch\":\"agent/task-1\"}"
run_status
check test "$status" -eq 0
check has 'Провайдер: GitLab https://gitlab.fixture.invalid/scm'
check has 'Репозиторий: group/sub/project'
check has 'Разрешённая ветка: agent/task-1'
check lacks "$token"
check read_only
check unchanged

write_config "{\"provider\":\"gitlab\",\"project\":\"group/sub/project\",\"token\":\"$token\",\"write\":false,\"writeBranch\":\"\"}"
run_status
check test "$status" -ne 0
check has 'Не удалось прочитать сохранённую настройку Git-шлюза vm-02.'
check lacks "$token"
check unchanged

check test "$(grep -rlE --exclude-dir=tests 'fixture_status_token' "$work/project" | wc -l)" -eq 0
printf 'test_vm_git_status: ok (%s)\n' "$(command -v make >/dev/null && echo make || echo 'bash v2/manage.sh без make')"
