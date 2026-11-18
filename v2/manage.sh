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
    mkdir -p "$VM_STATE/git" "$VM_STATE/git-mirror"
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
    mkdir -p "$VM_STATE"
    local candidate
    candidate=$(mktemp "$VM_STATE/.upstream.XXXXXX")
    trap 'rm -f -- "$candidate"' EXIT
    local response
    if ! IFS= read -rs -p "HTTP-прокси для $VM_NAME, включая логин и пароль при необходимости: " response </dev/tty; then
        fail 'Для настройки прокси нужен интерактивный терминал или готовый файл upstream.'
    fi
    printf '\n' >&2
    test -n "$response" || fail 'Адрес прокси обязателен.'
    printf '%s\n' "$response" > "$candidate"
    ensure_base_image "$candidate"
    verify_proxy "$candidate"
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
}
write_git_config() (
    local token candidate label=GitHub pattern='^[a-zA-Z0-9_-]{10,512}$' field=repository
    if [[ $provider = gitlab ]]; then label=GitLab pattern='^[a-zA-Z0-9_.-]{10,512}$' field=project; fi
    if ! IFS= read -rs -p "Отдельный токен $label для $repository: " token </dev/tty; then
        fail 'Для настройки Git нужен интерактивный терминал.'
    fi
    printf '\n' >&2
    [[ $token =~ $pattern ]] || fail "Недопустимый формат токена $label."
    mkdir -p "$VM_STATE/git"
    candidate=$(mktemp "$VM_STATE/git/.repository.XXXXXX")
    trap 'rm -f -- "$candidate"' EXIT
    printf '{"provider":"%s","origin":"%s","%s":"%s","token":"%s","write":%s,"writeBranch":"%s"}\n' \
        "$provider" "$origin" "$field" "$repository" "$token" "$writable" "$branch" > "$candidate"
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
    if [[ ${fields[0]:-} = github || ${fields[0]:-} = gitlab ]] && [[ ${fields[1]:-} = https://* ]]; then
        saved_provider=${fields[0]} saved_origin=${fields[1]} saved_repository=${fields[2]:-}
        saved_writable=${fields[3]:-} saved_branch=${fields[4]:-}
    else
        saved_provider=github saved_origin=https://github.com saved_repository=${fields[0]:-}
        saved_writable=${fields[1]:-} saved_branch=${fields[2]:-}
    fi
    [[ -n $saved_repository && ( $saved_writable = true || $saved_writable = false ) ]]
}
gateway_supports_gitlab() (
    local id target probe output
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
    printf '{"provider":"gitlab","origin":"https://gitlab.probe.invalid","project":"probe/project","token":"probe-token-00000000","write":false,"writeBranch":""}\n' \
        > "$probe/repository.json"
    output=$(docker run --rm --pull never --network none \
        --mount "type=bind,src=$probe/repository.json,dst=/probe.json,readonly" \
        "$target" node /opt/agent-docker/git-gateway.cjs --settings /probe.json 2>/dev/null) || exit 1
    [[ $output = 'gitlab https://gitlab.probe.invalid probe/project false ' ]]
)
require_gitlab_gateway() {
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
git_status() {
    local current saved_provider saved_origin saved_repository saved_writable saved_branch
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
    if [[ $saved_writable = true && -n $saved_branch ]]; then
        printf 'Режим: чтение и запись\nРазрешённая ветка: %s\n' "$saved_branch"
    elif [[ $saved_writable = true ]]; then
        printf 'Режим: только чтение, запись включена без допустимой ветки\nРазрешённая ветка: нет\n'
    else
        printf 'Режим: только чтение\nРазрешённая ветка: нет\n'
    fi
}
configure_git() (
    configured
    local provider origin repository mode branch writable
    git_settings
    require_gitlab_gateway
    write_git_config
)
setup_git() (
    local provider origin repository mode branch writable current update
    local saved_provider saved_origin saved_repository saved_writable saved_branch
    git_settings
    update="make vm-git VM=$VM_NAME REPO=$repository"
    if [[ $provider = gitlab ]]; then update+=" GIT_PROVIDER=gitlab GIT_URL=$origin"; fi
    if [[ $writable = true ]]; then update+=" GIT_WRITE=1 GIT_WRITE_BRANCH=$branch"; fi
    require_gitlab_gateway
    if ! test -e "$VM_STATE/git/repository.json"; then
        write_git_config create
        return
    fi
    if ! current=$(saved_git_settings) || ! parse_git_settings "$current"; then
        fail "Не удалось прочитать сохранённую настройку Git-шлюза $VM_NAME. Задайте её заново: $update"
    fi
    if [[ "$saved_provider $saved_origin $saved_repository $saved_writable $saved_branch" != \
        "$provider $origin $repository $writable $branch" ]]; then
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
        compose exec -T vm python3 /opt/agent-docker/poweroff.py
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
    mkdir -p "$VM_STATE"
    if ! test -f "$VM_STATE/resources"; then
        local avail available_ram
        avail=$(df -P -k "$state_root" | awk 'NR==2 {print $4}')
        (( avail >= 5 * 1024 * 1024 )) || fail 'Для подготовки освободите не менее 5 ГиБ на разделе с VM.'
        available_ram=$(awk '$1=="MemAvailable:" {print int($2 / 1024)}' /proc/meminfo)
        (( available_ram >= VM_CONFIG_RAM && VM_CONFIG_VCPU <= $(nproc) )) ||
            fail 'Доступных CPU или RAM недостаточно для выбранных настроек гостя.'
        printf '%s %s %s\n' "$VM_CONFIG_VCPU" "$VM_CONFIG_RAM" "$VM_CONFIG_DISK" > "$VM_STATE/resources"
    fi
    cp -n v2/allowed-domains.example "$VM_STATE/allowed-domains"
    chmod 0644 "$VM_STATE/allowed-domains"
    if ! test -f "$VM_STATE/upstream"; then configure_proxy; fi
    ensure_base_image "$VM_STATE/upstream"
    verify_proxy "$VM_STATE/upstream"
    docker run --rm --pull never --network none --cap-drop ALL --device /dev/kvm:/dev/kvm:rw \
        "$base_image" node -e 'require("node:fs").closeSync(require("node:fs").openSync("/dev/kvm", "r+"))' ||
        fail 'Docker не может открыть /dev/kvm.'
    build_image
    if test -n "${REPO:-}"; then setup_git; fi
    prepare_base
    if vm_running && compose exec -T vm sh -c 'grep -aFq "agent-docker-prepare.service: Failed" /guest/console.log'; then
        stop_guest
    fi
    start_guest
    wait_ready
}
configured() {
    test -f "$VM_STATE/resources" && test -f "$VM_STATE/upstream" ||
        fail "Сначала выполните make vm-setup VM=$VM_NAME."
    load_config
}

ensure_docker
set_name "${VM:-vm-01}"
case "$target" in
    vm-setup|init-claude) setup ;;
    vm-git) configure_git ;;
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
            test -d "$profile" || continue
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
        else
            limit=${VM_COUNT:-$(value VM_COUNT)}
            positive "$limit" && (( limit <= 254 )) || fail 'VM_COUNT должен быть от 1 до 254.'
            for ((index=1; index<=limit; index++)); do
                printf -v name 'vm-%02d' "$index"
                set_name "$name"
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
    vm-console|claude|claude-auth|claude-logout)
        configured
        start_guest
        wait_ready
        compose exec vm python3 /opt/agent-docker/console.py "$target" "${PROJECT:-}" "${RESUME:-}"
        ;;
    *) fail 'Неизвестная цель Make.' ;;
esac
