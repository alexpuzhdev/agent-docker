#!/bin/bash
# Сквозная проверка GitLab по HTTPS через upstream-прокси на подставном сервере.
# Настоящий GitLab не нужен: стенд воспроизводит его контракт Smart HTTP и
# авторизацию oauth2, а весь обмен идёт по loopback.
#
#   bash v2/tests/test_gitlab_https.sh
set -u
source_root=$(cd "$(dirname "$0")/../.." && pwd -P)
mirror_script=${1:-$source_root/v2/git-mirror.py}
backend=/usr/lib/git-core/git-http-backend
for tool in openssl git python3; do
    command -v "$tool" >/dev/null || { echo "Для теста нужен $tool." >&2; exit 1; }
done
test -x "$backend" || { echo "Для теста нужен git-http-backend." >&2; exit 1; }

# Настройки пользователя не должны влиять на тест: собственные вызовы git
# иначе наследуют ~/.gitconfig, где может быть, например, commit.gpgsign.
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0
unset GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL GIT_DIR GIT_WORK_TREE

real_git=$(command -v git)
work=$(mktemp -d)
cleanup() {
    for pid in ${server_pid:-} ${proxy_pid:-}; do kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; done
    rm -rf -- "$work"
}
trap cleanup EXIT
token=glpat-fixture-token-00000001
project=group/sub/project
real_branch=agent/work-2026
failures=0
label=''
check() {
    local name=${label:-$*}
    if "$@"; then
        printf 'OK     %s\n' "$name"
    else
        printf 'ПРОВАЛ (строка %s) %s\n' "${BASH_LINENO[1]}" "$name"
        failures=$((failures+1))
    fi
    label=''
}
named() { label=$1; shift; check "$@"; }
has() { grep -qF -- "$2" <<<"$1"; }
lacks() { ! grep -qF -- "$2" <<<"$1"; }

# Сертификат стенда и центр, который его выпустил.
mkdir -p "$work/tls" "$work/bin"
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$work/tls/ca.key" -out "$work/tls/ca.crt" \
    -days 1 -subj '/CN=agent-docker test CA' 2>/dev/null
openssl req -newkey rsa:2048 -nodes -keyout "$work/tls/server.key" -out "$work/tls/server.csr" \
    -subj '/CN=gitlab.localtest' 2>/dev/null
printf 'subjectAltName=DNS:gitlab.localtest\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n' > "$work/tls/ext"
openssl x509 -req -in "$work/tls/server.csr" -CA "$work/tls/ca.crt" -CAkey "$work/tls/ca.key" \
    -CAcreateserial -out "$work/tls/server.crt" -days 1 -extfile "$work/tls/ext" 2>/dev/null
cat "$work/tls/server.crt" "$work/tls/server.key" > "$work/tls/server.pem"

# Тестовому центру доверяют две настройки: обёртка git в PATH для прямых
# вызовов и http.sslCAInfo в зеркале для вызовов из hook, где git ставит
# свой каталог в начало PATH. Проверка TLS при этом остаётся включённой.
cat > "$work/bin/git" <<WRAP
#!/bin/bash
exec $real_git -c "http.sslCAInfo=\${AGD_TEST_CA:?}" "\$@"
WRAP
chmod +x "$work/bin/git"

cat > "$work/server.py" <<'PY'
import base64, http.server, os, ssl, subprocess, sys, threading
ROOT, CERT, TOKEN, LOG, PORT = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5])
BACKEND = '/usr/lib/git-core/git-http-backend'
lock = threading.Lock()


def record(line):
    with lock, open(LOG, 'a') as journal:
        journal.write(line + '\n')


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def authorized(self):
        header = self.headers.get('Authorization', '')
        if not header.startswith('Basic '):
            return False
        try:
            user, _, token = base64.b64decode(header[6:]).decode().partition(':')
        except ValueError:
            return False
        return user == 'oauth2' and token == TOKEN

    def read_body(self):
        # git переходит на Transfer-Encoding: chunked, когда тело больше
        # http.postBuffer, поэтому одного Content-Length недостаточно.
        if self.headers.get('Transfer-Encoding', '').lower() == 'chunked':
            chunks = []
            while True:
                size = int(self.rfile.readline().split(b';')[0], 16)
                if not size:
                    while self.rfile.readline().strip():
                        pass
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)
            return b''.join(chunks)
        length = int(self.headers.get('Content-Length', 0))
        return self.rfile.read(length) if length else b''

    def serve(self):
        if not self.authorized():
            record(f'401 {self.command} {self.path}')
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="GitLab"')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        record(f'{self.command} {self.path}')
        path, _, query = self.path.partition('?')
        body = self.read_body()
        length = len(body)
        environment = {'PATH': os.environ['PATH'], 'GIT_PROJECT_ROOT': ROOT,
                       'GIT_HTTP_EXPORT_ALL': '1', 'REQUEST_METHOD': self.command,
                       'PATH_INFO': path, 'QUERY_STRING': query,
                       'CONTENT_TYPE': self.headers.get('Content-Type', ''),
                       'CONTENT_LENGTH': str(length), 'REMOTE_USER': 'oauth2',
                       'REMOTE_ADDR': '127.0.0.1',
                       'HTTP_CONTENT_ENCODING': self.headers.get('Content-Encoding', '')}
        result = subprocess.run([BACKEND], env=environment, input=body, capture_output=True)
        if result.returncode:
            record(f'500 {self.command} {path}')
            self.send_response(500)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        headers, _, payload = result.stdout.partition(b'\r\n\r\n')
        status, sent = 200, []
        for line in headers.decode().split('\r\n'):
            name, _, value = line.partition(': ')
            if name.lower() == 'status':
                status = int(value.split()[0])
            elif name:
                sent.append((name, value))
        self.send_response(status)
        for name, value in sent:
            self.send_header(name, value)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = serve
    do_POST = serve


context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(CERT)
server = http.server.ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
PY

cat > "$work/proxy.py" <<'PY'
import socket, socketserver, sys, threading
TARGET_PORT, LISTEN_PORT, LOG = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
ALLOWED = 'gitlab.localtest:443'
lock = threading.Lock()


def record(line):
    with lock, open(LOG, 'a') as journal:
        journal.write(line + '\n')


def pump(source, destination):
    try:
        while True:
            data = source.recv(65536)
            if not data:
                break
            destination.sendall(data)
    except OSError:
        pass
    finally:
        for side in (source, destination):
            try:
                side.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class Proxy(socketserver.StreamRequestHandler):
    def handle(self):
        request = self.rfile.readline().decode(errors='replace').strip()
        while self.rfile.readline().strip():
            pass
        method, _, rest = request.partition(' ')
        authority = rest.partition(' ')[0]
        record(f'{method} {authority}')
        if method != 'CONNECT' or authority != ALLOWED:
            self.wfile.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
            return
        try:
            upstream = socket.create_connection(('127.0.0.1', TARGET_PORT), timeout=10)
        except OSError:
            self.wfile.write(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n')
            return
        self.wfile.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
        self.wfile.flush()
        threads = [threading.Thread(target=pump, args=pair, daemon=True)
                   for pair in ((self.connection, upstream), (upstream, self.connection))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


Server(('127.0.0.1', LISTEN_PORT), Proxy).serve_forever()
PY

echo '=== Репозиторий на стороне GitLab ==='
upstream=$work/repos/$project.git
mkdir -p "$upstream"
git init -q --bare "$upstream"
seed=$work/seed
git init -q -b main "$seed"
(
    cd "$seed" || exit 1
    git config user.name 'Мария Иванова'; git config user.email 'maria@corp.example'
    mkdir -p .gitlab src
    printf '## Шаблон\n' > .gitlab/issue_templates.md
    printf 'stages:\n  - build\n' > .gitlab-ci.yml
    printf '* @corp/platform\n' > CODEOWNERS
    printf 'print("сервис")\n' > src/app.py
    git add -A; git commit -q -m 'Первый коммит от Марии'
    git config user.name 'Пётр Петров'; git config user.email 'petr@corp.example'
    printf 'print("обновление")\n' > src/app.py
    git add -A; git commit -q -m 'Правка от Петра'
    git tag v1.0
    git checkout -q -b "$real_branch"
    printf 'временная ветка\n' > branch.txt
    git add -A; git commit -q -m 'Коммит в рабочей ветке'
    git checkout -q main
    git push -q "$upstream" main "$real_branch" v1.0
)
# У настоящего GitLab HEAD указывает на ветку по умолчанию.
git -C "$upstream" symbolic-ref HEAD refs/heads/main
named 'репозиторий провайдера подготовлен' \
    test -n "$(git -C "$upstream" rev-parse -q --verify "refs/heads/$real_branch")"

echo '=== Запуск стенда ==='
read -r https_port proxy_port <<<"$(python3 - <<'PY'
import socket
probes = []
for _ in range(2):
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    probes.append(probe)
print(*(probe.getsockname()[1] for probe in probes))
for probe in probes:
    probe.close()
PY
)"
journal=$work/requests.log
: > "$journal"
python3 "$work/server.py" "$work/repos" "$work/tls/server.pem" "$token" "$journal" "$https_port" >/dev/null 2>&1 &
server_pid=$!
python3 "$work/proxy.py" "$https_port" "$proxy_port" "$journal" >/dev/null 2>&1 &
proxy_pid=$!
for _ in $(seq 1 40); do
    if timeout 2 bash -c "exec 3<>/dev/tcp/127.0.0.1/$https_port" 2>/dev/null &&
       timeout 2 bash -c "exec 3<>/dev/tcp/127.0.0.1/$proxy_port" 2>/dev/null; then break; fi
    sleep 0.25
done
named 'стенд поднялся' kill -0 "$server_pid" "$proxy_pid"
printf 'http://127.0.0.1:%s\n' "$proxy_port" > "$work/upstream"
printf '{"provider":"gitlab","origin":"https://gitlab.localtest","project":"%s","token":"%s","write":true,"writeBranch":"%s"}\n' \
    "$project" "$token" "$real_branch" > "$work/repository.json"
export AGD_TEST_CA="$work/tls/ca.crt" PATH="$work/bin:$PATH"
mirror_env=(GIT_MIRROR_ROOT="$work/mirror" GIT_MIRROR_CONFIG="$work/repository.json"
            GIT_MIRROR_UPSTREAM="$work/upstream" AGD_TEST_CA="$work/tls/ca.crt" PATH="$PATH")

echo '=== Синхронизация зеркала ==='
sync_output=$(env "${mirror_env[@]}" timeout 120 python3 "$mirror_script" sync 2>&1); sync_status=$?
named 'синхронизация зеркала прошла' test "$sync_status" -eq 0
clean=$(find "$work/mirror/mirrors" -maxdepth 2 -name clean.git -type d 2>/dev/null | head -1)
named 'зеркало создано' test -n "$clean"
test -n "$clean" || { echo "Вывод sync: $sync_output" >&2; echo "=== Итог: неудачных проверок $failures ==="; exit 1; }
source_git=$(find "$work/mirror/mirrors" -maxdepth 2 -name source.git -type d | head -1)
git -C "$source_git" config http.sslCAInfo "$work/tls/ca.crt"

echo '=== Что видит агент ==='
guest=$work/guest
git clone -q "$clean" "$guest"
refs=$(git ls-remote "$clean" | awk '{print $2}' | sort | tr '\n' ' ')
named 'гость видит только HEAD, main и work' test "$refs" = 'HEAD refs/heads/main refs/heads/work '
authors=$(git -C "$guest" log --all --format='%an <%ae>|%cn <%ce>' | tr '|' '\n' | sort -u | tr '\n' ' ')
named 'единственный автор служебный' test "$authors" = 'agent-docker <agent-docker@localhost> '
dates=$(git -C "$guest" log --all --format='%at' | sort -u | tr '\n' ' ')
named 'дата коммитов фиксированная' test "$dates" = '946684800 '
named 'нет .gitlab-ci.yml' test ! -e "$guest/.gitlab-ci.yml"
named 'нет CODEOWNERS' test ! -e "$guest/CODEOWNERS"
named 'нет каталога .gitlab' test ! -e "$guest/.gitlab"
named 'исходники на месте' test -f "$guest/src/app.py"
messages=$(git -C "$guest" log --all --format='%s' | sort -u | tr '\n' ' ')
named 'сообщения коммитов провайдера скрыты' lacks "$messages" 'Марии'
named 'история провайдера скрыта' lacks "$messages" 'Петра'
named 'имя рабочей ветки не попало в клон' \
    bash -c '! grep -rqF "$1" "$2/.git"' _ "$real_branch" "$guest"
named 'теги не видны' test -z "$(git -C "$guest" tag)"

echo '=== Запись из гостя ==='
(
    cd "$guest" || exit 1
    git checkout -q work
    printf 'проверка GitLab\n' > gitlab-check.txt
    git add -A
    git -c user.name='Агент' -c user.email='agent@guest' commit -q -m 'Проверка записи через шлюз'
)
push_output=$(cd "$guest" && env "${mirror_env[@]}" timeout 120 git push "$clean" work 2>&1); push_status=$?
named 'push из гостя принят' test "$push_status" -eq 0
test "$push_status" -eq 0 || printf 'вывод push:\n%s\n' "$push_output" >&2
main_before=$(git -C "$upstream" rev-parse main)
upstream_log=$(git -C "$upstream" log -1 --format='%s|%an <%ae>|%cn <%ce>' "$real_branch")
named 'коммит дошёл до реальной ветки' has "$upstream_log" 'Проверка записи через шлюз'
named 'коммит подписан служебной личностью' has "$upstream_log" 'agent-docker <agent-docker@localhost>'
named 'личность гостя не ушла провайдеру' lacks "$upstream_log" 'agent@guest'
named 'ветка по умолчанию не тронута' test "$(git -C "$upstream" rev-parse main)" = "$main_before"

echo '=== Путь запроса ==='
requests=$(cat "$journal")
named 'запрос шёл через upstream-прокси' has "$requests" 'CONNECT gitlab.localtest:443'
named 'путь с подгруппой построен верно' has "$requests" "GET /$project.git/info/refs?service=git-upload-pack"
named 'запись шла по git-receive-pack' has "$requests" "POST /$project.git/git-receive-pack"
named 'в успешном пути не было 401' lacks "$requests" '401 '

echo '=== Отказы ==='
bad=$work/bad
mkdir -p "$bad"
sed 's/"token":"[^"]*"/"token":"glpat-wrong-token-99999999"/' \
    "$work/repository.json" > "$bad/repository.json"
bad_output=$(env GIT_MIRROR_ROOT="$bad/mirror" GIT_MIRROR_CONFIG="$bad/repository.json" \
    GIT_MIRROR_UPSTREAM="$work/upstream" AGD_TEST_CA="$work/tls/ca.crt" PATH="$PATH" \
    timeout 60 python3 "$mirror_script" sync 2>&1); bad_status=$?
named 'неверный токен отклонён' test "$bad_status" -ne 0
named 'токен не попал в сообщение об ошибке' lacks "$bad_output" 'glpat-wrong-token-99999999'
named 'адрес сервера не раскрыт при отказе' lacks "$bad_output" 'gitlab.localtest'
named 'имя проекта не раскрыто при отказе' lacks "$bad_output" "$project"
named 'сервер ответил 401 на чужой токен' has "$(cat "$journal")" '401 GET'

openssl req -x509 -newkey rsa:2048 -nodes -keyout "$work/tls/other.key" -out "$work/tls/other.crt" \
    -days 1 -subj '/CN=посторонний центр' 2>/dev/null
other=$work/other
mkdir -p "$other"
cp "$work/repository.json" "$other/repository.json"
other_output=$(env GIT_MIRROR_ROOT="$other/mirror" GIT_MIRROR_CONFIG="$other/repository.json" \
    GIT_MIRROR_UPSTREAM="$work/upstream" AGD_TEST_CA="$work/tls/other.crt" PATH="$PATH" \
    timeout 60 python3 "$mirror_script" sync 2>&1); other_status=$?
named 'сертификат стенда от чужого центра не принимается' test "$other_status" -ne 0

kill "$server_pid" 2>/dev/null; wait "$server_pid" 2>/dev/null; server_pid=''
down=$work/down
mkdir -p "$down"
cp "$work/repository.json" "$down/repository.json"
down_output=$(env GIT_MIRROR_ROOT="$down/mirror" GIT_MIRROR_CONFIG="$down/repository.json" \
    GIT_MIRROR_UPSTREAM="$work/upstream" AGD_TEST_CA="$work/tls/ca.crt" PATH="$PATH" \
    timeout 60 python3 "$mirror_script" sync 2>&1); down_status=$?
named 'недоступный сервер даёт отказ' test "$down_status" -ne 0
named 'адрес не раскрыт при недоступности' lacks "$down_output" 'gitlab.localtest'
named 'токен не раскрыт при недоступности' lacks "$down_output" "$token"

echo "=== Итог: неудачных проверок $failures ==="
exit $(( failures > 0 ? 1 : 0 ))
