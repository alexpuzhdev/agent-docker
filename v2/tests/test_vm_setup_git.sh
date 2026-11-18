#!/bin/bash
# Проверка настройки Git-шлюза из make vm-setup с подставным docker и временным состоянием.
set -euo pipefail
source_root=$(cd "$(dirname "$0")/../.." && pwd -P)
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
unset REPO GIT_WRITE GIT_WRITE_BRANCH VM VCPU RAM_MB DISK_GB
for tool in node script setsid; do command -v "$tool" >/dev/null || { echo "Для теста нужен $tool." >&2; exit 1; }; done

mkdir -p "$work/project" "$work/bin" "$work/state/vm-02"
cp -R "$source_root/Makefile" "$source_root/.env.example" "$source_root/v2" "$work/project/"
printf '1 512 4\n' > "$work/state/vm-02/resources"
printf 'http://proxy.invalid:3128\n' > "$work/state/vm-02/upstream"
config=$work/state/vm-02/git/repository.json
cat > "$work/bin/docker" <<'EOF'
#!/bin/bash
git=no
if test -f "$FAKE_GIT_CONFIG"; then git=yes; fi
printf 'git=%s %s\n' "$git" "$*" >> "$FAKE_DOCKER_LOG"
case " $* " in
    *' ps -q '*|*' ps -a -q '*) echo fake-container ;;
    *' exec '*) [[ $* == *AGENT_DOCKER_READY* ]] ;;
    *' --settings /repository.json '*) node "$work/project/v2/git-gateway.cjs" --settings "$FAKE_GIT_CONFIG" ;;
esac
EOF
chmod +x "$work/bin/docker"
export work SHELL=/bin/bash PATH="$work/bin:$PATH" V2_STATE_DIR="$work/state" FAKE_GIT_CONFIG="$config" FAKE_DOCKER_LOG="$work/docker.log"

target() {
    if command -v make >/dev/null; then
        make -s -C "$work/project" "$@"
    else
        (cd "$work/project" && env "${@:2}" bash v2/manage.sh "$1")
    fi
}
run_no_tty() {
    : > "$FAKE_DOCKER_LOG"
    set +e
    output=$(setsid -w bash -c "$(declare -f target); target \"\$@\"" _ "$@" 2>&1 </dev/null)
    status=$?
    set -e
}
run_tty() {
    local input=$1 log=$work/tty.log
    shift
    : > "$FAKE_DOCKER_LOG"
    : > "$log"
    set +e
    {
        for _ in $(seq 1 100); do grep -q 'токен GitHub' "$log" && { printf '%s\n' "$input"; break; }; sleep 0.1; done
    } | script -qfec "$(declare -f target); target $(printf '%q ' "$@")" "$log" >/dev/null
    status=$?
    set -e
    output=$(cat "$log")
}
check() { "$@" || { printf 'Провал (строка %s): %s\nКод: %s\nВывод:\n%s\n' "${BASH_LINENO[0]}" "$*" "$status" "$output" >&2; exit 1; }; }
has() { grep -qF -- "$1" <<<"$output"; }
docker_line() { grep -F -- "$1" "$FAKE_DOCKER_LOG" | head -1; }
token_read=fixture_read_token_0001
token_write=fixture_write_token_0002

run_no_tty vm-setup VM=vm-02
check test "$status" -eq 0
check has 'vm-02 готов.'
check test ! -e "$config"
check test -n "$(docker_line ' up -d ')"

run_no_tty vm-setup VM=vm-02 REPO=owner/repository
check test "$status" -ne 0
check has 'нужен интерактивный терминал'
check test ! -e "$config"
check test -z "$(find "$work/state/vm-02" -name '.repository.*')"
check test -z "$(docker_line ' up -d ')"

run_tty "$token_read" vm-setup VM=vm-02 REPO=owner/repository
check test "$status" -eq 0
check has 'токен GitHub'
check has 'Git-шлюз настроен для vm-02.'
check test "$(stat -c %a "$work/state/vm-02/git")" = 700
check has 'vm-02 готов.'
check test "$(cat "$config")" = '{"repository":"owner/repository","token":"fixture_read_token_0001","write":false,"writeBranch":""}'
check test "$(stat -c %a "$config")" = 600
check test "$(docker_line check-upstream.cjs | cut -d' ' -f1)" = git=no
check test "$(docker_line /dev/kvm:/dev/kvm | cut -d' ' -f1)" = git=no
check test "$(docker_line ' up -d ' | cut -d' ' -f1)" = git=yes
check test "$(grep -c "$token_read" <<<"$output")" -eq 0
check test "$(grep -c "$token_read" "$FAKE_DOCKER_LOG")" -eq 0
cp "$config" "$work/saved.json"

run_no_tty vm-setup VM=vm-02 REPO=owner/repository
check test "$status" -eq 0
check has 'Сохранённая настройка используется без изменений.'
check has 'vm-02 готов.'
check cmp -s "$config" "$work/saved.json"

run_no_tty vm-setup VM=vm-02 REPO=owner/other
check test "$status" -ne 0
check has 'make vm-git VM=vm-02 REPO=owner/other'
check test -z "$(docker_line ' up -d ')"
check cmp -s "$config" "$work/saved.json"

run_no_tty vm-setup VM=vm-02 REPO=owner/repository GIT_WRITE=1 GIT_WRITE_BRANCH=test/gateway-write
check test "$status" -ne 0
check has 'make vm-git VM=vm-02 REPO=owner/repository GIT_WRITE=1 GIT_WRITE_BRANCH=test/gateway-write'
check test -z "$(docker_line ' up -d ')"
check cmp -s "$config" "$work/saved.json"

run_no_tty vm-setup VM=vm-02 REPO=owner/repository GIT_WRITE=1
check test "$status" -ne 0
check has 'GIT_WRITE_BRANCH'
check test -z "$(docker_line ' up -d ')"
check cmp -s "$config" "$work/saved.json"

run_tty "$token_write" vm-git VM=vm-02 REPO=owner/repository GIT_WRITE=1 GIT_WRITE_BRANCH=test/gateway-write
check test "$status" -eq 0
check has 'токен GitHub'
check has 'Git-шлюз настроен для vm-02.'
check test "$(cat "$config")" = '{"repository":"owner/repository","token":"fixture_write_token_0002","write":true,"writeBranch":"test/gateway-write"}'
check test "$(grep -c "$token_write" <<<"$output")" -eq 0

run_no_tty vm-setup VM=vm-02 REPO=owner/repository GIT_WRITE=1 GIT_WRITE_BRANCH=test/gateway-write
check test "$status" -eq 0
check has 'Сохранённая настройка используется без изменений.'

run_no_tty vm-setup VM=vm-02 REPO=owner/repository
check test "$status" -ne 0
check has 'make vm-git VM=vm-02 REPO=owner/repository'

cat > "$config" <<'JSON'
{
  "write": false,
  "token": "fixture_read_token_0001",
  "repository": "owner/repository"
}
JSON
cp "$config" "$work/saved.json"
run_no_tty vm-setup VM=vm-02 REPO=owner/repository
check test "$status" -eq 0
check has 'Сохранённая настройка используется без изменений.'
check cmp -s "$config" "$work/saved.json"

printf 'broken\n' > "$config"
cp "$config" "$work/saved.json"
run_no_tty vm-setup VM=vm-02 REPO=owner/repository
check test "$status" -ne 0
check has 'Не удалось прочитать сохранённую настройку'
check cmp -s "$config" "$work/saved.json"

check test -f "$work/project/.env"
check test "$(grep -rlE --exclude-dir=tests 'fixture_(read|write)_token' "$work/project" | wc -l)" -eq 0
check test -z "$(find "$work/state/vm-02/git" -name '.repository.*')"
printf 'test_vm_setup_git: ok (%s)\n' "$(command -v make >/dev/null && echo make || echo 'bash v2/manage.sh без make')"
