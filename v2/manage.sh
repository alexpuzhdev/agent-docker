#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
root=$(pwd -P)
state_root=${V2_STATE_DIR:-$root/.v2-state}
image=agent-docker-v2:local
base_image=node:22-bookworm-slim
export V2_BASE_VOLUME=${V2_BASE_VOLUME:-agent-docker-v2-base}
project_prefix=${V2_PROJECT_PREFIX:-agent-docker-v2}
target=$1
umask 077
mkdir -p "$state_root"
state_root=$(cd "$state_root" && pwd -P)
cp -n .env.example .env

value() {
    awk -F= -v key="$1" '$1==key { sub(/^[^=]*=/, ""); print; exit }' .env
}
fail() { printf '%s\n' "$*" >&2; exit 1; }
positive() { [[ $1 =~ ^[1-9][0-9]{0,5}$ ]]; }
compose() {
    local GIT_CONTAINER_CONFIG="$VM_STATE/git" GIT_CONTAINER_MIRROR="$VM_STATE/git-mirror" GIT_HOST_PUBLISHED_ROOT= GIT_HOST_PUBLISH_SOCKET=
    mkdir -p "$GIT_CONTAINER_CONFIG" "$GIT_CONTAINER_MIRROR" "$VM_STATE/git-host-public" "$VM_STATE/git-host-ipc"
    if test -f "$VM_STATE/git-host-public/current" || test -f "$VM_STATE/git-host-private/filter-required"; then
        GIT_CONTAINER_CONFIG="$VM_STATE/git-host-empty/config"
        GIT_CONTAINER_MIRROR="$VM_STATE/git-host-empty/mirror"
        GIT_HOST_PUBLISHED_ROOT=/host-git
        GIT_HOST_PUBLISH_SOCKET=/host-git-ipc/publish.sock
        mkdir -p "$GIT_CONTAINER_CONFIG" "$GIT_CONTAINER_MIRROR"
    fi
    export GIT_CONTAINER_CONFIG GIT_CONTAINER_MIRROR GIT_HOST_PUBLISHED_ROOT GIT_HOST_PUBLISH_SOCKET
    docker compose --project-name "$project_prefix-$VM_NAME" -f "$root/v2/compose.yaml" "$@"
}
set_name() {
    VM_NAME=$1
    [[ $VM_NAME =~ ^vm-([0-9]{2,3})$ ]] || fail 'Выберите VM в формате vm-01.'
    local number=$((10#${BASH_REMATCH[1]}))
    (( number >= 1 && number <= 254 )) || fail 'Допустимы номера VM от 01 до 254.'
    local prefix=${VM_NETWORK_PREFIX:-$(value VM_NETWORK_PREFIX)}
    [[ $prefix =~ ^10\.([0-9]{1,3})$ ]] && (( 10#${BASH_REMATCH[1]} <= 255 )) ||
        fail 'VM_NETWORK_PREFIX должен иметь формат 10.число.'
    export VM_NAME VM_STATE="$state_root/$VM_NAME" VM_SUBNET="$prefix.$number.0/28" GATEWAY_IP="$prefix.$number.2"
    export HOST_UID="$(id -u)" HOST_GID="$(id -g)"
}
load_config() {
    local cpu ram disk
    if test -f "$VM_STATE/resources"; then
        read -r cpu ram disk < "$VM_STATE/resources"
        if [[ ${VCPU:-$cpu} != "$cpu" || ${RAM_MB:-$ram} != "$ram" || ${DISK_GB:-$disk} != "$disk" ]]; then
            fail 'Для этого гостя уже сохранены ресурсы. Запустите его с сохранёнными значениями.'
        fi
    else
        cpu=${VCPU:-${VM_VCPU:-$(value VM_VCPU)}}
        ram=${RAM_MB:-${VM_RAM_MB:-$(value VM_RAM_MB)}}
        disk=${DISK_GB:-${VM_DISK_GB:-$(value VM_DISK_GB)}}
    fi
    positive "$cpu" && positive "$ram" && positive "$disk" || fail 'CPU, RAM и диск должны быть положительными числами.'
    (( ram >= 512 && disk >= 4 )) || fail 'Для Debian задайте хотя бы 512 МиБ RAM и диск от 4 ГиБ.'
    export VM_CONFIG_VCPU=$cpu VM_CONFIG_RAM=$ram VM_CONFIG_DISK=$disk
}
verify_proxy() {
    local file=$1
    docker run --rm --pull never \
        --mount "type=bind,src=$root/v2,dst=/scripts,readonly" \
        --mount "type=bind,src=$file,dst=/upstream,readonly" \
        "$base_image" node /scripts/check-upstream.cjs /upstream
}
ensure_docker() {
    docker info --format '{{.ServerVersion}}' >/dev/null 2>&1 ||
        fail 'Docker daemon недоступен текущему пользователю.'
    docker compose version >/dev/null || fail 'Установите Docker Compose v2.'
}
ensure_base_image() {
    local file=$1
    if docker image inspect "$base_image" >/dev/null 2>&1; then return; fi
    command -v curl >/dev/null || fail 'Загрузите базовый образ Docker из среды с прокси либо установите curl для проверки прокси.'
    local proxy
    proxy=$(cat "$file")
    HTTPS_PROXY=$proxy https_proxy=$proxy curl -q --silent --fail --noproxy '' --max-time 15 \
        "https://downloads.claude.ai/claude-code/apt/stable/dists/stable/InRelease" -o /dev/null ||
        fail 'Прокси недоступен. Скачивание образа отменено.'
    test "$(docker info --format '{{if .HTTPSProxy}}yes{{end}}')" = yes ||
        fail 'Для загрузки базового образа настройте HTTPS-прокси Docker daemon либо загрузите образ из доверенной среды.'
    docker pull "$base_image"
}
configure_proxy() (
    local candidate
    candidate=$(mktemp "$state_root/.upstream.XXXXXX")
    trap 'rm -f -- "$candidate"' EXIT
    local response
    if ! IFS= read -rs -p "HTTP-прокси для $VM_NAME, включая логин и пароль при необходимости: " response </dev/tty; then
        fail 'Для настройки прокси нужен интерактивный терминал или готовый файл upstream.'
    fi
    printf '\n' >&2
    test -n "$response" || fail 'Адрес прокси обязателен.'
    if [[ $response != *://* ]]; then response="http://$response"; fi
    printf '%s\n' "$response" > "$candidate"
    ensure_base_image "$candidate"
    verify_proxy "$candidate"
    mkdir -p "$VM_STATE"
    mv -f "$candidate" "$VM_STATE/upstream"
)
git_settings() {
    provider=${GIT_PROVIDER:-github}
    origin=${GIT_URL:-}
    repository=${REPO:-}
    mode=${GIT_WRITE:-0}
    branch=${GIT_WRITE_BRANCH:-}
    case $provider in
        github)
            [[ -z $origin || $origin = https://github.com ]] || fail 'GIT_URL задаётся только вместе с GIT_PROVIDER=gitlab.'
            origin=https://github.com
            [[ $repository =~ ^[a-zA-Z0-9][a-zA-Z0-9-]{0,38}/[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$ && $repository != *.git ]] ||
                fail 'Укажите GitHub-репозиторий как REPO=owner/name.'
            ;;
        gitlab)
            origin=${origin%/}
            local host=${origin#https://}
            host=${host%%[:/]*}
            [[ ${#origin} -le 512 && $host != github.com && $host != *.github.com &&
                $origin =~ ^https://([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z]([a-z0-9-]{0,61}[a-z0-9])?(:([1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5]))?(/[a-zA-Z0-9_~-][a-zA-Z0-9_.~-]*)*$ ]] ||
                fail 'Укажите адрес сервера GitLab по DNS-имени как GIT_URL=https://gitlab.example.com.'
            [[ ${#repository} -le 1024 && $repository != *.git && $repository != *.atom &&
                $repository =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,254}(/[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,254}){1,19}$ ]] ||
                fail 'Укажите проект GitLab как REPO=group/subgroup/project.'
            ;;
        *) fail 'Укажите GIT_PROVIDER=github или GIT_PROVIDER=gitlab.' ;;
    esac
    [[ $mode = 0 || $mode = 1 ]] || fail 'Укажите GIT_WRITE=0 или GIT_WRITE=1.'
    if [[ $mode = 1 ]]; then
        [[ ${#branch} -le 120 && $branch != main && $branch != master &&
            $branch =~ ^([a-zA-Z0-9][a-zA-Z0-9_-]*/)*[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]] ||
            fail 'Для записи укажите отдельную ветку через GIT_WRITE_BRANCH, например test/gateway-write.'
    else
        branch=''
    fi
    writable=false
    if [[ $mode = 1 ]]; then writable=true; fi
    git_read_mapping
}

git_read_mapping() {
    local entry alias upstream
    local -a entries sorted
    local -A aliases=()
    read_aliases=''
    read_config='{}'
    test -n "${GIT_READ_BRANCHES:-}" || return 0
    [[ $GIT_READ_BRANCHES != ,* && $GIT_READ_BRANCHES != *, ]] ||
        fail 'Укажите ветки чтения в формате имя=ветка,имя=ветка.'
    IFS=, read -r -a entries <<<"$GIT_READ_BRANCHES"
    mapfile -t sorted < <(printf '%s\n' "${entries[@]}" | LC_ALL=C sort)
    read_config='{'
    for entry in "${sorted[@]}"; do
        alias=${entry%%=*}
        upstream=${entry#*=}
        [[ $entry = *=* && $alias =~ ^[a-z][a-z0-9-]{0,31}$ && $alias != main && $alias != work &&
            ${#upstream} -le 120 && $upstream =~ ^([a-zA-Z0-9][a-zA-Z0-9_-]*/)*[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]] ||
            fail 'Укажите ветки чтения в формате имя=ветка. Имена main и work зарезервированы.'
        [[ ! -v "aliases[$alias]" ]] || fail 'Имена веток чтения должны быть уникальны.'
        aliases[$alias]=1
        read_aliases+="${read_aliases:+,}$entry"
        read_config+="\"$alias\":\"$upstream\","
    done
    read_config="${read_config%,}}"
}
write_git_config() (
    local token candidate label=GitHub pattern='^[a-zA-Z0-9_-]{10,512}$' field=repository mapping=''
    if [[ $provider = gitlab ]]; then label=GitLab pattern='^[a-zA-Z0-9_.-]{10,512}$' field=project; fi
    if ! IFS= read -rs -p "Отдельный токен $label для $repository: " token </dev/tty; then
        fail 'Для настройки Git нужен интерактивный терминал.'
    fi
    printf '\n' >&2
    [[ $token =~ $pattern ]] || fail "Недопустимый формат токена $label."
    mkdir -p "$VM_STATE/git"
    candidate=$(mktemp "$VM_STATE/git/.repository.XXXXXX")
    trap 'rm -f -- "$candidate"' EXIT
    if test -n "$read_aliases"; then mapping=',"readBranches":'"$read_config"; fi
    printf '{"provider":"%s","origin":"%s","%s":"%s","token":"%s","write":%s,"writeBranch":"%s"%s}\n' \
        "$provider" "$origin" "$field" "$repository" "$token" "$writable" "$branch" "$mapping" > "$candidate"
    if [[ ${1:-} = create ]]; then
        ln -T -- "$candidate" "$VM_STATE/git/repository.json" ||
            fail "Git-шлюз $VM_NAME уже настроен параллельной командой. Повторите vm-setup."
    else
        mv -f -- "$candidate" "$VM_STATE/git/repository.json"
    fi
    printf 'Git-шлюз настроен для %s. Адрес в госте: http://git.agent-docker.test/project.git\n' "$VM_NAME"
)
parse_git_settings() {
    local -a fields
    read -r -a fields <<<"$1"
    saved_reads=''
    if [[ ${fields[0]:-} = github || ${fields[0]:-} = gitlab ]] && [[ ${fields[1]:-} = https://* ]]; then
        saved_provider=${fields[0]} saved_origin=${fields[1]} saved_repository=${fields[2]:-}
        saved_writable=${fields[3]:-} saved_branch=${fields[4]:-}
        saved_reads=${fields[5]:-}
        if [[ $saved_branch = '-' ]]; then saved_branch=''; fi
    else
        saved_provider=github saved_origin=https://github.com saved_repository=${fields[0]:-}
        saved_writable=${fields[1]:-} saved_branch=${fields[2]:-}
    fi
    [[ -n $saved_repository && ( $saved_writable = true || $saved_writable = false ) ]]
}
gateway_supports_gitlab() (
    local id target probe output
    local operation=--settings expected='gitlab https://gitlab.probe.invalid probe/project false ' branches='{}'
    id=$(compose ps -q proxy 2>/dev/null || true)
    if [[ -n $id ]]; then
        target=$(docker inspect --format '{{.Image}}' "$id") || exit 1
    else
        target=$image
    fi
    docker image inspect "$target" >/dev/null 2>&1 || exit 1
    mkdir -p "$VM_STATE/git"
    probe=$(mktemp -d "$VM_STATE/git/.probe.XXXXXX")
    trap 'rm -rf -- "$probe"' EXIT
    if [[ ${1:-} = history ]]; then
        operation=--read-branches
        expected=reference=branch-source
        branches='{"reference":"branch-source"}'
    fi
    printf '{"provider":"gitlab","origin":"https://gitlab.probe.invalid","project":"probe/project","token":"probe-token-00000000","write":false,"writeBranch":"","readBranches":%s}\n' "$branches" \
        > "$probe/repository.json"
    output=$(docker run --rm --pull never --network none \
        --mount "type=bind,src=$probe/repository.json,dst=/probe.json,readonly" \
        "$target" node /opt/agent-docker/git-gateway.cjs "$operation" /probe.json 2>/dev/null) || exit 1
    [[ $output = "$expected" ]]
)
require_gitlab_gateway() {
    if [[ -n $read_aliases ]] && ! gateway_supports_gitlab history; then
        fail "Образ Git-шлюза $VM_NAME не поддерживает настройку веток чтения. Обновите образ перед настройкой."
    fi
    if [[ $provider = gitlab ]] && ! gateway_supports_gitlab; then
        fail "Образ Git-шлюза $VM_NAME не поддерживает GitLab. Выполните make vm-stop VM=$VM_NAME и make vm-setup VM=$VM_NAME, затем повторите настройку."
    fi
}
saved_git_settings() {
    docker run --rm --pull never --network none \
        --mount "type=bind,src=$root/v2,dst=/scripts,readonly" \
        --mount "type=bind,src=$VM_STATE/git/repository.json,dst=/repository.json,readonly" \
        "$base_image" node /scripts/git-gateway.cjs --settings /repository.json
}
host_git_proxy_active() {
    local id
    id=$(compose ps -q proxy 2>/dev/null) || return 1
    test -n "$id" || return 1
    local binds
    binds=$(docker inspect --format '{{range .Mounts}}{{println .Destination .Source .RW}}{{end}}' "$id") ||
        return 1
    grep -Fxq "/host-git $VM_STATE/git-host-public false" <<<"$binds" &&
        grep -Fxq "/config/git $VM_STATE/git-host-empty/config false" <<<"$binds" &&
        grep -Fxq "/mirror $VM_STATE/git-host-empty/mirror true" <<<"$binds"
}

git_status() {
    local current saved_provider saved_origin saved_repository saved_writable saved_branch saved_reads
    if ! test -e "$VM_STATE/git/repository.json" && ! test -L "$VM_STATE/git/repository.json"; then
        printf 'Git-шлюз %s не настроен. Назначьте репозиторий: make vm-git VM=%s REPO=owner/name\n' "$VM_NAME" "$VM_NAME"
        return
    fi
    current=$(saved_git_settings) || {
        (( $? == 1 )) &&
            fail "Не удалось прочитать сохранённую настройку Git-шлюза $VM_NAME. Задайте её заново: make vm-git VM=$VM_NAME REPO=owner/name"
        fail "Не удалось проверить настройку Git-шлюза $VM_NAME: Docker не запустил парсер из образа $base_image."
    }
    parse_git_settings "$current" ||
        fail "Не удалось прочитать сохранённую настройку Git-шлюза $VM_NAME. Задайте её заново: make vm-git VM=$VM_NAME REPO=owner/name"
    if [[ $saved_provider = gitlab ]]; then
        printf 'Git-шлюз %s\nПровайдер: GitLab %s\nРепозиторий: %s\n' "$VM_NAME" "$saved_origin" "$saved_repository"
    else
        printf 'Git-шлюз %s\nПровайдер: GitHub\nРепозиторий: %s\n' "$VM_NAME" "$saved_repository"
    fi
    if test -f "$VM_STATE/git-host-public/current"; then
        if host_git_proxy_active; then
            if test -S "$VM_STATE/git-host-ipc/publish.sock" &&
                ! flock -n "$VM_STATE/git-host-private/writer.lock" -c ':'; then
                printf 'Режим: зеркало хоста, чтение и публикация в work.\n'
            else
                printf 'Режим: зеркало хоста, только чтение.\n'
            fi
        else
            printf 'Зеркало опубликовано. Для подключения выполните atrice git host-activate %s.\n' "$VM_NAME"
        fi
    elif test -f "$VM_STATE/git-host-private/filter-required"; then
        printf 'Фильтр включён. Проверенное зеркало пока не опубликовано.\n'
    elif test -d "$VM_STATE/git-host-empty/config" && host_git_proxy_active; then
        printf 'Зеркало хоста отключено. Пересоздайте контейнер шлюза для возврата к обычному режиму.\n'
    elif [[ $saved_writable = true && -n $saved_branch ]]; then
        printf 'Режим: чтение и запись\nРазрешённая ветка: %s\n' "$saved_branch"
    elif [[ $saved_writable = true ]]; then
        printf 'Режим: только чтение, запись включена без допустимой ветки\nРазрешённая ветка: нет\n'
    else
        printf 'Режим: только чтение\nРазрешённая ветка: нет\n'
    fi
    if test -n "$saved_reads"; then printf 'Ветки чтения: %s\n' "$saved_reads"; fi
}
host_git_sync() (
    configured
    local config="$VM_STATE/git/repository.json" private="$VM_STATE/git-host-private"
    local public="$VM_STATE/git-host-public" location draft work old current pointer= view filter= audit= backup
    test -f "$config" || fail 'Сначала назначьте репозиторий через atrice git.'
    mkdir -p "$private" "$public"
    if [[ ${1:-} != locked ]]; then
        exec 9>"$private/publish.lock"
        flock 9
    fi
    work=$(mktemp -d "$private/job.XXXXXXXXXX")
    draft=
    trap 'rm -rf -- "$work" "$draft" "$pointer"' EXIT
    draft=$(mktemp -d "$work/view-XXXXXXXXXX")
    cp -- "$config" "$work/repository.json"
    if test -f "$private/filter-rules.json"; then
        filter="$work/filter-rules.json"
        cp -- "$private/filter-rules.json" "$filter"
        touch "$private/filter-required"
        if test -f "$public/current"; then
            read -r backup < "$public/current"
            if [[ ! $backup =~ ^view-[a-zA-Z0-9]{10}$ ]] || ! test -f "$public/$backup/.filtered"; then
                rm -f -- "$public/current"
            fi
        fi
        for view in "$public"/view-*; do
            [[ ${view##*/} =~ ^view-[a-zA-Z0-9]{10}$ ]] || continue
            if ! test -f "$view/.filtered"; then mv -T -- "$view" "$work/${view##*/}"; fi
        done
        audit="$work/audit-rules.json"
        if test -f "$private/audit-rules.json"; then
            cp -- "$private/audit-rules.json" "$audit"
        else
            printf '{}\n' > "$audit"
        fi
    elif test -f "$private/filter-required"; then
        fail 'Правила фильтрации удалены. Восстановите filter-rules.json перед обновлением.'
    fi
    if ! location=$(env -u GIT_MIRROR_REMOTE -u GIT_MIRROR_UPSTREAM \
        GIT_HOST_DIRECT=1 GIT_HOST_CONTENT_POLICY="$filter" \
        GIT_MIRROR_ROOT="$private/engine" GIT_MIRROR_CONFIG="$work/repository.json" \
        python3 "$root/v2/git-mirror.py" sync); then
        fail 'Хост не смог обновить Git-зеркало. Новая копия не опубликована.'
    fi
    [[ $location =~ ^mirrors/[0-9a-f]{64}$ ]] || fail 'Не удалось определить путь очищенного зеркала.'
    local -a isolated_git=(env -i PATH="$PATH" GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null git)
    "${isolated_git[@]}" init -q --bare --template= --initial-branch=main "$draft/clean.git"
    "${isolated_git[@]}" -C "$draft/clean.git" fetch -q --atomic --no-tags --no-write-fetch-head \
        "$private/engine/$location/clean.git" 'refs/heads/*:refs/heads/*'
    cmp -s -- "$config" "$work/repository.json" ||
        fail 'Назначение Git-шлюза изменилось во время синхронизации. Повторите команду.'
    if test -n "$filter"; then
        if ! python3 "$root/v2/host/git_audit.py" "$public" "$work/repository.json" \
            "$audit" "$private/audit-candidate.json" --candidate "$draft/clean.git"; then
            fail "Новая копия не опубликована. Отчёт: $private/audit-candidate.json"
        fi
        cmp -s -- "$filter" "$private/filter-rules.json" || fail 'Правила фильтрации изменились. Повторите команду.'
        if test -f "$private/audit-rules.json" && ! cmp -s "$audit" "$private/audit-rules.json"; then
            fail 'Правила аудита изменились. Повторите команду.'
        fi
        touch "$draft/.filtered"
    fi
    old=
    if test -f "$public/current"; then read -r old < "$public/current"; fi
    current=${draft##*/}
    [[ $(stat -c %d "$draft") = "$(stat -c %d "$public")" ]] ||
        fail 'Для публикации приватный и публичный каталоги должны находиться на одной файловой системе.'
    mv -T -- "$draft" "$public/$current"
    pointer=$(mktemp "$public/.current.XXXXXXXXXX")
    printf '%s\n' "$current" > "$pointer"
    mv -T -- "$pointer" "$public/current"
    draft=
    pointer=
    for view in "$public"/view-*; do
        [[ ${view##*/} =~ ^view-[a-zA-Z0-9]{10}$ ]] || continue
        if [[ ${view##*/} != "$current" && ( -n $filter || ${view##*/} != "$old" ) ]]; then rm -rf -- "$view"; fi
    done
    printf '%s: очищенное зеркало обновлено на хосте.\n' "$VM_NAME"
    if test -z "$old"; then
        printf 'Для подключения выполните atrice git host-activate %s.\n' "$VM_NAME"
    fi
)

host_git_worker() (
    configured
    local private="$VM_STATE/git-host-private" ipc="$VM_STATE/git-host-ipc"
    test -f "$VM_STATE/git-host-public/current" || fail 'Сначала обновите зеркало через atrice git host-sync.'
    GIT_MIRROR_CONFIG="$VM_STATE/git/repository.json" python3 "$root/v2/git-mirror.py" host-check-write
    mkdir -p "$private" "$ipc"
    exec 8>"$private/writer.lock"
    flock -n 8 || fail 'Публикация этой ВМ уже запущена.'
    export VM=$VM_NAME
    exec python3 "$root/v2/host/git-push-worker.py" "$ipc/publish.sock" "$private" "$root/v2/manage.sh"
)

host_git_receive() (
    local generation=$1 old=$2 new=$3 pack=$4 current private="$VM_STATE/git-host-private"
    local public="$VM_STATE/git-host-public" filter= audit=
    exec 9>"$private/publish.lock"
    flock 9
    read -r current < "$public/current" || fail 'Зеркало ожидает синхронизации.'
    test "$generation" = "$current" || fail 'Поколение зеркала изменилось.'
    if test -f "$private/filter-rules.json"; then
        filter="$private/filter-rules.json"
    elif test -f "$private/filter-required"; then
        fail 'Правила фильтрации отсутствуют.'
    fi
    if test -f "$private/audit-rules.json"; then audit="$private/audit-rules.json"; fi
    env -u GIT_MIRROR_REMOTE -u GIT_MIRROR_UPSTREAM GIT_HOST_DIRECT=1 \
        GIT_HOST_CONTENT_POLICY="$filter" GIT_HOST_AUDIT_RULES="$audit" \
        GIT_MIRROR_ROOT="$private/engine" GIT_MIRROR_CONFIG="$VM_STATE/git/repository.json" \
        python3 "$root/v2/git-mirror.py" host-receive "$old" "$new" "$pack"
    host_git_sync locked
)

audit_git_publication() (
    local private="$VM_STATE/git-host-private" public="$VM_STATE/git-host-public"
    test -f "$public/current" || fail 'Сначала создайте зеркало через atrice git host-sync.'
    exec 9>"$private/publish.lock"
    flock 9
    python3 "$root/v2/host/git_audit.py" "$public" "$VM_STATE/git/repository.json" \
        "$private/audit-rules.json" "$private/audit-report.json"
)

host_git_activate() (
    configured
    test -d "$VM_STATE/git-host-private" || fail 'Сначала обновите зеркало через atrice git host-sync.'
    exec 9>"$VM_STATE/git-host-private/publish.lock"
    flock 9
    test -f "$VM_STATE/git-host-public/current" || fail 'Сначала обновите зеркало через atrice git host-sync.'
    local proxy_image
    proxy_image=$(compose config --images proxy)
    if ! docker run --rm --pull never --network none "$proxy_image" \
        node /opt/agent-docker/git-gateway.cjs --host-write; then
        fail 'Установите образ шлюза с поддержкой зеркала хоста.'
    fi
    compose up -d --no-deps --no-build --pull never --force-recreate proxy
    host_git_proxy_active || fail 'Контейнер шлюза не подхватил зеркало хоста.'
    local id
    id=$(compose ps -q proxy)
    docker inspect --format '{{range .Mounts}}{{println .Destination .Source .RW}}{{end}}' "$id" |
        grep -Fxq "/host-git-ipc $VM_STATE/git-host-ipc false" ||
        fail 'Контейнер шлюза не подключил каталог публикации.'
    printf '%s: зеркало хоста подключено. Для записи запустите atrice git host-writer.\n' "$VM_NAME"
)

configure_git() (
    configured
    mkdir -p "$VM_STATE/git-host-private"
    exec 9>"$VM_STATE/git-host-private/publish.lock"
    flock 9
    test ! -f "$VM_STATE/git-host-public/current" ||
        fail 'Сначала отключите зеркало хоста командой atrice git host-disable.'
    local provider origin repository mode branch writable read_aliases read_config
    git_settings
    require_gitlab_gateway
    write_git_config
)
setup_git() (
    local provider origin repository mode branch writable current update read_aliases read_config
    local saved_provider saved_origin saved_repository saved_writable saved_branch saved_reads
    git_settings
    update="make vm-git VM=$VM_NAME REPO=$repository"
    if [[ $provider = gitlab ]]; then update+=" GIT_PROVIDER=gitlab GIT_URL=$origin"; fi
    if [[ $writable = true ]]; then update+=" GIT_WRITE=1 GIT_WRITE_BRANCH=$branch"; fi
    if test -n "$read_aliases"; then update+=" GIT_READ_BRANCHES=$read_aliases"; fi
    require_gitlab_gateway
    if ! test -e "$VM_STATE/git/repository.json"; then
        write_git_config create
        return
    fi
    if ! current=$(saved_git_settings) || ! parse_git_settings "$current"; then
        fail "Не удалось прочитать сохранённую настройку Git-шлюза $VM_NAME. Задайте её заново: $update"
    fi
    if [[ "$saved_provider $saved_origin $saved_repository $saved_writable $saved_branch $saved_reads" != \
        "$provider $origin $repository $writable $branch $read_aliases" ]]; then
        fail "Для $VM_NAME уже настроен Git-шлюз с другим провайдером, репозиторием или режимом записи. Сохранённая настройка не изменена. Чтобы заменить её, выполните: $update"
    fi
    printf 'Git-шлюз %s уже настроен для %s. Сохранённая настройка используется без изменений.\n' "$VM_NAME" "$repository"
)
build_image() {
    local source_hash existing proxy
    source_hash=$(find v2 -maxdepth 1 -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)
    existing=$(docker image inspect --format '{{index .Config.Labels "agent-docker-v2.source"}}' "$image" 2>/dev/null || true)
    if [[ $existing == "$source_hash" ]]; then return; fi
    proxy=$(cat "$VM_STATE/upstream")
    if ! HTTP_PROXY=$proxy HTTPS_PROXY=$proxy http_proxy=$proxy https_proxy=$proxy NO_PROXY= no_proxy= DOCKER_BUILDKIT=0 \
        docker build --pull=false --build-arg HTTP_PROXY --build-arg HTTPS_PROXY \
        --build-arg http_proxy --build-arg https_proxy --build-arg NO_PROXY --build-arg no_proxy \
        --label "agent-docker-v2.source=$source_hash" -t "$image" "$root/v2"; then
        fail 'Образ зависимостей не собрался через прокси.'
    fi
}
prepare_base() {
    local proxy
    proxy=$(cat "$VM_STATE/upstream")
    docker volume create "$V2_BASE_VOLUME" >/dev/null
    HTTP_PROXY=$proxy HTTPS_PROXY=$proxy http_proxy=$proxy https_proxy=$proxy NO_PROXY= no_proxy= \
        docker run --rm --pull never \
        --env HTTP_PROXY --env HTTPS_PROXY --env http_proxy --env https_proxy --env NO_PROXY --env no_proxy \
        --mount "type=volume,src=$V2_BASE_VOLUME,dst=/images" "$image" bash /opt/agent-docker/prepare-base.sh
}
vm_running() { test -n "$(compose ps -q vm)"; }
stop_guest() {
    if vm_running; then
        # Явный возврат вместо errexit: внутри условия errexit отключается,
        # и отказ выключения иначе остался бы незамеченным.
        compose exec -T vm python3 /opt/agent-docker/poweroff.py || return 1
        timeout 50 docker wait "$(compose ps -a -q vm)" >/dev/null ||
            fail 'Гость не завершил штатную остановку.'
    fi
}
start_guest() {
    if vm_running; then
        compose up -d --pull never --no-build --no-recreate
    else
        compose up -d --pull never --no-build
    fi
}
# open_console открывает консоль гостя и прибирает её за собой.
#
# Закрытый хостовый терминал изнутри гостя не виден: docker exec с tty
# оставляет мастер pty открытым на стороне демона, никакого события в гостя не
# приходит, и console.py остаётся висеть, держа /guest/console.lock. Следующий
# вход получал «Консоль этого гостя уже открыта», и освобождал блокировку
# только оператор вручную. Знает об уходе клиента хост, поэтому он же об этом
# и сообщает: каждому входу выдаётся токен, а ловушка на HUP, TERM и выходе
# просит гостя освободить консоль ровно с этим токеном.
#
# Освобождение избирательное и тихое: чужую или новую консоль оно не трогает,
# потому что токен не совпадёт, а при свободной блокировке не делает ничего.
# Гостю при этом не отправляется ни один байт, поэтому запущенный агент и
# сессии tmux продолжают работать — закрытие клиента их не касается.
# Сброс приватных режимов терминала хоста: мышь, скобочная вставка и
# альтернативный экран. Нужен на случай, когда консоль в контейнере не успела
# сбросить их сама — например её прервали вместе с docker exec. В гостя не
# уходит ничего, и при выводе не в терминал не пишется вовсе.
reset_terminal() {
    test -t 1 || return 0
    printf '\033[?1000l\033[?1001l\033[?1002l\033[?1003l\033[?1005l\033[?1006l\033[?1015l\033[?1016l\033[?2004l\033[?1049l'
}

open_console() {
    local sample token
    # Токен берётся без конвейера. Привычное tr -dc ... | head -c 32 под
    # set -euo pipefail обрывает tr сигналом SIGPIPE, конвейер отдаёт 141, и
    # errexit заканчивал цель до запуска консоли: вход в гостя не открывался
    # вовсе. od читает ровно 16 байт сам, а лишние символы снимает подстановка.
    sample=$(LC_ALL=C od -An -vtx1 -N16 /dev/urandom)
    token=${sample//[^0-9a-f]/}
    test ${#token} -eq 32 || fail 'Не удалось получить токен консоли.'
    trap "compose exec -T vm python3 /opt/agent-docker/console.py --release '' $token || true
        reset_terminal" HUP TERM EXIT
    # MOUSE=off оставляет мышь терминалу хоста: включения отслеживания
    # снимаются из потока, и обычное выделение текста работает.
    compose exec -e AGENT_DOCKER_CONSOLE_TOKEN="$token" \
        -e AGENT_DOCKER_CONSOLE_MOUSE="${MOUSE:-app}" vm \
        python3 /opt/agent-docker/console.py "$1" "${PROJECT:-}" "${RESUME:-}"
}
wait_ready() {
    local attempt
    for attempt in $(seq 1 150); do
        if test -z "$(compose ps -q vm)"; then
            compose logs --tail 25 vm >&2 || true
            fail "$VM_NAME остановилась при загрузке."
        fi
        if compose exec -T vm sh -c 'grep -aFq AGENT_DOCKER_READY /guest/console.log' >/dev/null 2>&1; then
            printf '%s готов.\n' "$VM_NAME"
            return
        fi
        if compose exec -T vm sh -c 'grep -aEq "установка отменена|agent-docker-prepare.service: Failed" /guest/console.log' >/dev/null 2>&1; then
            break
        fi
        sleep 2
    done
    compose exec -T vm tail -35 /guest/console.log >&2 || true
    fail "Подготовка $VM_NAME не завершилась. Повторите make vm-setup после устранения ошибки."
}
setup() {
    test -c /dev/kvm || fail 'Docker-хосту нужен /dev/kvm.'
    load_config
    if ! test -f "$VM_STATE/resources"; then
        local avail available_ram
        avail=$(df -P -k "$state_root" | awk 'NR==2 {print $4}')
        (( avail >= 5 * 1024 * 1024 )) || fail 'Для подготовки освободите не менее 5 ГиБ на разделе с VM.'
        available_ram=$(awk '$1=="MemAvailable:" {print int($2 / 1024)}' /proc/meminfo)
        (( available_ram >= VM_CONFIG_RAM && VM_CONFIG_VCPU <= $(nproc) )) ||
            fail 'Доступных CPU или RAM недостаточно для выбранных настроек гостя.'
    fi
    if test -f "$VM_STATE/upstream"; then
        ensure_base_image "$VM_STATE/upstream"
        verify_proxy "$VM_STATE/upstream"
    else
        configure_proxy
    fi
    docker run --rm --pull never --network none --cap-drop ALL --device /dev/kvm:/dev/kvm:rw \
        "$base_image" node -e 'require("node:fs").closeSync(require("node:fs").openSync("/dev/kvm", "r+"))' ||
        fail 'Docker не может открыть /dev/kvm.'
    if ! test -f "$VM_STATE/resources"; then
        printf '%s %s %s\n' "$VM_CONFIG_VCPU" "$VM_CONFIG_RAM" "$VM_CONFIG_DISK" > "$VM_STATE/resources"
    fi
    cp -n v2/allowed-domains.example "$VM_STATE/allowed-domains"
    chmod 0644 "$VM_STATE/allowed-domains"
    build_image
    if test -n "${REPO:-}"; then setup_git; fi
    prepare_base
    if vm_running && compose exec -T vm sh -c 'grep -aFq "agent-docker-prepare.service: Failed" /guest/console.log'; then
        stop_guest
    fi
    start_guest
    wait_ready
    rm -f -- "$VM_STATE/removed"
}
show_logs() {
    local tail=${LOG_TAIL:-50} service=${LOG_SERVICE:-}
    local -a arguments=(logs --no-color)
    [[ $tail = all || $tail =~ ^(0|[1-9][0-9]{0,5})$ ]] || fail 'LOG_TAIL принимает число или all.'
    arguments+=(--tail "$tail")
    case $service in
        '') ;;
        vm|proxy) ;;
        *) fail 'LOG_SERVICE принимает vm или proxy.' ;;
    esac
    if [[ ${LOG_FOLLOW:-0} = 1 ]]; then arguments+=(--follow); fi
    if test -n "$service"; then arguments+=("$service"); fi
    compose "${arguments[@]}"
}
show_profile() {
    vm_running || fail "$VM_NAME не запущена. Профиль читается из работающего гостя: make vm-up VM=$VM_NAME"
    compose exec -T vm cat /guest/profile.json
}
remove_guest() {
    # Проверка до первого вызова compose: он создаёт каталоги состояния и
    # после него несуществующий гость выглядел бы настроенным.
    test "$VM_STATE" = "$state_root/$VM_NAME" || fail 'Каталог состояния VM не распознан.'
    test -d "$VM_STATE" || fail "$VM_NAME не найдена: каталог состояния отсутствует."
    if ! (stop_guest); then
        printf 'Штатное выключение %s не завершилось, контейнеры останавливаются принудительно.\n' "$VM_NAME" >&2
    fi
    if [[ ${DISK_KEEP:-1} = 0 ]]; then
        compose down --volumes --remove-orphans
        rm -rf -- "$VM_STATE"
        printf '%s удалена вместе с диском и настройками.\n' "$VM_NAME"
    else
        compose down --remove-orphans
        # Запись об открытой сессии описывает гостя, которого больше нет.
        rm -f -- "$VM_STATE/session"
        touch "$VM_STATE/removed"
        printf '%s убрана из списка. Диск и настройки сохранены. Вернуть: atrice setup %s\n' "$VM_NAME" "$VM_NAME"
    fi
}
configured() {
    test -f "$VM_STATE/resources" && test -f "$VM_STATE/upstream" ||
        fail "Сначала выполните make vm-setup VM=$VM_NAME."
    load_config
}

if [[ $target = vm-git-audit ]]; then
    test -n "${VM:-}" || fail 'Укажите ВМ явно.'
    set_name "$VM"
    audit_git_publication
    exit
fi

if [[ $target = vm-git-host-receive ]]; then
    test "$#" -eq 5 || fail 'Некорректный запрос публикации.'
    set_name "${VM:?}"
    host_git_receive "$2" "$3" "$4" "$5"
    exit
fi

ensure_docker
set_name "${VM:-vm-01}"
case "$target" in
    vm-setup|init-claude) setup ;;
    vm-git) configure_git ;;
    vm-git-host-sync) host_git_sync ;;
    vm-git-host-writer) host_git_worker ;;
    vm-git-host-activate) host_git_activate ;;
    vm-git-host-disable)
        test -n "${VM:-}" || fail 'Укажите ВМ явно.'
        if test -d "$VM_STATE/git-host-private"; then
            (
                flock 9
                rm -f -- "$VM_STATE/git-host-public/current"
            ) 9>"$VM_STATE/git-host-private/publish.lock"
        fi
        if test -f "$VM_STATE/git-host-private/filter-required"; then
            printf 'Зеркало отключено. Для восстановления обновите его с действующими правилами фильтрации.\n'
        else
            printf 'Зеркало хоста отключено. Настройка вступит в силу после пересоздания контейнера шлюза.\n'
        fi
        ;;
    vm-git-status) git_status ;;
    vm-proxy)
        configure_proxy
        if test -f "$VM_STATE/resources" && docker image inspect "$image" >/dev/null 2>&1; then
            load_config
            if test -n "$(compose ps -q proxy)"; then
                compose up -d --no-build --pull never --force-recreate proxy
            fi
        fi
        ;;
    vm-list)
        for profile in "$state_root"/vm-*; do
            test -d "$profile" && ! test -f "$profile/removed" || continue
            name=${profile##*/}
            running=$(docker ps --filter "label=com.docker.compose.project=$project_prefix-$name" \
              --filter label=com.docker.compose.service=vm --format '{{.Status}}')
            printf '%s\t%s\n' "$name" "${running:-остановлена}"
        done
        ;;
    vm-up)
        if test -n "${VM:-}"; then
            configured
            verify_proxy "$VM_STATE/upstream"
            start_guest
            rm -f -- "$VM_STATE/removed"
        else
            limit=${VM_COUNT:-$(value VM_COUNT)}
            positive "$limit" && (( limit <= 254 )) || fail 'VM_COUNT должен быть от 1 до 254.'
            for ((index=1; index<=limit; index++)); do
                printf -v name 'vm-%02d' "$index"
                set_name "$name"
                if test -f "$VM_STATE/removed"; then continue; fi
                if test -f "$VM_STATE/resources" && test -f "$VM_STATE/upstream"; then
                    load_config
                    if verify_proxy "$VM_STATE/upstream"; then start_guest; fi
                else
                    printf '%s пропущена. Сначала выполните make vm-setup VM=%s.\n' "$name" "$name"
                fi
            done
        fi
        ;;
    vm-stop)
        configured
        stop_guest
        compose stop proxy
        ;;
    vm-logs)
        configured
        show_logs
        ;;
    vm-profile)
        configured
        show_profile
        ;;
    vm-remove)
        test -n "${VM:-}" || fail 'Укажите гостя явно: make vm-remove VM=vm-02.'
        # Бюджет не проверяется: при удалении значения нужны только для
        # подстановки в compose.yaml, а сохранённые ресурсы могут быть
        # несовместимы с окружением или повреждены незавершённой настройкой.
        export VM_CONFIG_VCPU=1 VM_CONFIG_RAM=512 VM_CONFIG_DISK=4
        remove_guest
        ;;
    vm-sessions)
        configured
        # Канал сессий только читает список и не касается пользовательской
        # консоли, поэтому гостя здесь не поднимаем: у остановленного живых
        # сессий нет по определению.
        vm_running || fail "$VM_NAME остановлена: живых сессий нет."
        compose exec -T vm python3 /opt/agent-docker/sessions.py sessions
        ;;
    vm-sessions-check)
        configured
        # Диагностика подключения: одно состояние на выходе, в JSON. Она
        # ничего не запускает, в пользовательскую консоль не пишет и не
        # падает на старом образе — показать состояние должен список на
        # хосте, а не отказ. Состояние самого гостя хост знает сам, поэтому
        # у остановленного диагностика не запрашивается.
        vm_running || fail "$VM_NAME остановлена: диагностика канала ей не нужна."
        compose exec -T vm python3 /opt/agent-docker/sessions.py diagnose
        ;;
    vm-session-open)
        configured
        test -n "${RESUME:-}" ||
            fail "Укажите живую сессию: RESUME=<имя> VM=$VM_NAME bash v2/manage.sh vm-session-open."
        # Подключение к уже существующей сессии ничего не поднимает и никого не
        # запускает: у остановленной ВМ живых сессий нет по определению, а
        # поднимать её ради attach нельзя — просили сессию, а не нового агента.
        # Поэтому здесь нет ни start_guest, ни wait_ready.
        vm_running || fail "$VM_NAME остановлена: живых сессий в ней нет."
        open_console "$target"
        ;;
    vm-session-direct-check)
        configured
        # Читающая проверка прямого пути: ready — служба отвечает, silent —
        # порт есть, а службы в госте нет, absent — порта нет вовсе (гость
        # запущен прежней сборкой). Ни консоли, ни канала списка не касается.
        vm_running || fail "$VM_NAME остановлена: прямого пути в её сессии нет."
        compose exec -T vm python3 /opt/agent-docker/session.py probe
        ;;
    vm-session-direct|vm-session-new)
        configured
        # Прямой вход в управляемую сессию: отдельный порт virtio-serial и
        # собственный клиент tmux в госте. Serial-консоль не затрагивается
        # вовсе, её блокировка не берётся, поэтому вход работает и при занятой
        # оболочке. Гостя цель не поднимает: сессии живут только в работающем.
        vm_running || fail "$VM_NAME остановлена: живых сессий в ней нет."
        if test "$target" = vm-session-new && test -z "${RESUME:-}"; then
            # Имя уникально и берётся без конвейера: tr -dc ... | head -c под
            # set -euo pipefail обрывается SIGPIPE и заканчивает цель.
            sample=$(LC_ALL=C od -An -vtx1 -N8 /dev/urandom)
            suffix=${sample//[^0-9a-f]/}
            test ${#suffix} -eq 16 || fail 'Не удалось получить имя новой сессии.'
            RESUME="claude-$suffix"
            printf 'Новая сессия: %s\n' "$RESUME"
        fi
        test -n "${RESUME:-}" ||
            fail "Укажите сессию: RESUME=<имя> VM=$VM_NAME bash v2/manage.sh vm-session-direct."
        # Токен этого входа — тем же способом без конвейера, что и у консоли.
        # Ушедший клиент docker exec изнутри контейнера не виден: реле
        # оставалось работать и подтверждать путь keepalive, сессия висела
        # подключённой, а проверка пути отвечала silent до ручного
        # завершения процесса. Поэтому про уход сообщает хост ловушкой, а
        # освобождается ровно реле этого входа — по токену.
        sample=$(LC_ALL=C od -An -vtx1 -N16 /dev/urandom)
        token=${sample//[^0-9a-f]/}
        test ${#token} -eq 32 || fail 'Не удалось получить токен входа в сессию.'
        trap "compose exec -T vm python3 /opt/agent-docker/session.py --release '' $token || true
            reset_terminal" HUP TERM EXIT
        compose exec -e AGENT_DOCKER_SESSION_TIMEOUT="${SESSION_TIMEOUT:-5}" \
            -e AGENT_DOCKER_CONSOLE_MOUSE="${MOUSE:-app}" \
            -e AGENT_DOCKER_SESSION_TOKEN="$token" vm \
            python3 /opt/agent-docker/session.py open '' "$RESUME"
        ;;
    vm-shell-check)
        configured
        # Читающая диагностика serial-оболочки: ждёт ли она ввод или в ней
        # работает приложение. Ни блокировки, ни сокета консоли, ни tty этот
        # режим не касается, поэтому его можно спрашивать при живом агенте.
        vm_running || fail "$VM_NAME остановлена: её оболочку диагностировать нечем."
        compose exec -T vm python3 /opt/agent-docker/console.py --shell '' ''
        ;;
    vm-session-claude)
        configured
        # Новая управляемая сессия рядом с уже работающими. Гостя здесь не
        # поднимаем и нового гостя не ждём: просили сессию рядом с живой, а у
        # остановленной ВМ живых сессий нет — для неё остаётся обычная цель
        # claude. Прежние сессии цель не трогает: tmux создаёт ещё одну на том
        # же общем сокете.
        vm_running || fail "$VM_NAME остановлена: запустите её или откройте агента целью claude."
        if test -z "${RESUME:-}"; then
            # Имя уникально и берётся без конвейера: tr -dc ... | head -c под
            # set -euo pipefail обрывается SIGPIPE и заканчивает цель.
            sample=$(LC_ALL=C od -An -vtx1 -N8 /dev/urandom)
            suffix=${sample//[^0-9a-f]/}
            test ${#suffix} -eq 16 || fail 'Не удалось получить имя новой сессии.'
            RESUME="claude-$suffix"
        fi
        printf 'Новая сессия: %s\n' "$RESUME"
        open_console "$target"
        ;;
    vm-console|claude|claude-auth|claude-logout)
        configured
        start_guest
        wait_ready
        rm -f -- "$VM_STATE/removed"
        open_console "$target"
        ;;
    *) fail 'Неизвестная цель Make.' ;;
esac
