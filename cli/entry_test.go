package main

import (
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// typeKeys подменяет чтение клавиш заданной последовательностью: экраны
// списка и сессий проверяются без tty.
func typeKeys(t *testing.T, typed ...string) {
	t.Helper()
	keys := make([]rune, 0, len(typed))
	for _, press := range typed {
		keys = append(keys, decodeKey([]byte(press)))
	}
	saved := readMenuKey
	position := 0
	readMenuKey = func() (rune, error) {
		if position >= len(keys) {
			return 0, io.EOF
		}
		key := keys[position]
		position++
		return key, nil
	}
	t.Cleanup(func() { readMenuKey = saved })
}

// runningGuest готовит проект с одним запущенным гостем и отчётом канала.
func runningGuest(t *testing.T, report string) (string, string) {
	t.Helper()
	root, log := fakeProject(t, 0)
	state := t.TempDir()
	writeVM(t, state, "vm-01", "2 4096 12\n", "")
	t.Setenv("V2_STATE_DIR", state)
	t.Setenv("V2_PROJECT_PREFIX", "стенд")
	fakeDocker(t, "стенд", "vm-01", "Up 7 minutes")
	fakeChannel(t, root, report)
	return root, log
}

// Enter открывает список живых сессий гостя, а не запускает агента вслепую:
// именно так срывался вход — в занятой оболочке гость отвечает отказом.
// С экрана сессий c открывает консоль, a запускает агента, а Ctrl+] и q
// возвращают в список.
func TestSessionsScreenKeysChooseTarget(t *testing.T) {
	root, _ := runningGuest(t, liveFixture(liveLive, liveTwoSessions))
	quiet := streams{errors: io.Discard, output: io.Discard}
	cases := []struct {
		name     string
		keys     []string
		expected string
		session  string
	}{
		{"консоль по c", []string{"c"}, "vm-console", ""},
		{"консоль по русской с", []string{"с"}, "vm-console", ""},
		// a на экране сессий создаёт новую сессию рядом с живыми, а не
		// входит в существующую: имя ей даёт manage.sh, поэтому RESUME пуст.
		{"новая сессия по a", []string{"a"}, "vm-session-claude", ""},
		{"новая сессия по русской ф", []string{"ф"}, "vm-session-claude", ""},
		{"возврат по q", []string{"q"}, "", ""},
		{"возврат по русской й", []string{"й"}, "", ""},
		{"возврат по Ctrl+]", []string{"\x1d"}, "", ""},
		{"возврат по Esc", []string{"\x1b"}, "", ""},
		{"обновление и возврат", []string{"r", "\x1d"}, "", ""},
		// Enter подтверждает подсвеченную сессию. Первая в фикстуре уже
		// подключена: её клиент tmux занимает оболочку гостя, поэтому
		// возвращаются в неё консолью — так же выглядит возврат после Ctrl+].
		{"подключённая сессия по CR", []string{"\r"}, "vm-console", ""},
		{"подключённая сессия по LF", []string{"\n"}, "vm-console", ""},
		{"вниз и вверх возвращают выбор", []string{"j", "k", "\r"}, "vm-console", ""},
		// Вторая отсоединена: к ней подключаются целью vm-session-open.
		{"отсоединённая сессия стрелкой CSI", []string{"\x1b[B", "\r"}, "vm-session-open", "20261122T2030-11ab"},
		{"отсоединённая сессия стрелкой SS3", []string{"\x1bOB", "\r"}, "vm-session-open", "20261122T2030-11ab"},
		{"отсоединённая сессия по LF", []string{"j", "\n"}, "vm-session-open", "20261122T2030-11ab"},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			typeKeys(t, test.keys...)
			output := &strings.Builder{}
			console := streams{output: output, errors: io.Discard}
			target, session, err := sessionsScreen(root, "vm-01", console, quiet)
			if err != nil {
				t.Fatal(err)
			}
			if target != test.expected || session != test.session {
				t.Fatalf("выбрана цель %q сессии %q, ожидались %q и %q",
					target, session, test.expected, test.session)
			}
			if !strings.Contains(output.String(), "живые сессии") {
				t.Fatalf("экран сессий не нарисован: %s", output)
			}
		})
	}
}

// Экран сессий показывает только то, что ответил канал: отчёт берётся по
// цели vm-sessions-check, и журнал вызовов целей от него не меняется.
func TestSessionsScreenReadsChannelOnly(t *testing.T) {
	root, log := runningGuest(t, liveFixture(liveLive, liveSessionFixture))
	typeKeys(t, "\x1d")
	output := &strings.Builder{}
	console := streams{output: output, errors: io.Discard}
	if _, _, err := sessionsScreen(root, "vm-01", console, console); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(log); err == nil {
		t.Fatal("опрос канала не должен запускать меняющие цели")
	}
	for _, expected := range []string{"20261123T0915-9f3c", "claude", "подсказка канала"} {
		if !strings.Contains(output.String(), expected) {
			t.Fatalf("на экране нет %q: %s", expected, output)
		}
	}
}

// Возврат в гостя выбирается по живым сессиям, а не по памяти CLI. Это и
// есть исправленный сбой входа: записи о прошлом входе в состоянии нет, в
// госте работает агент, и вслепую запущенный второй агент получал отказ.
func TestResumeFollowsChannelSessions(t *testing.T) {
	root, log := runningGuest(t, liveFixture(liveLive, liveSessionFixture))
	console, _, errorsOutput := quietStreams("")
	if code := execute([]string{"--root", root, "go", "vm-01"}, console); code != 0 {
		t.Fatalf("код %d, ошибки: %s", code, errorsOutput)
	}
	recorded, err := os.ReadFile(log)
	if err != nil {
		t.Fatal(err)
	}
	if string(recorded) != "target=vm-console\nVM=vm-01\n" {
		t.Fatalf("при живых сессиях atrice go вызвал:\n%s", recorded)
	}
}

// Пустой список канала означает, что в госте ничего не работает: там
// запускается агент, даже если память CLI помнит прошлый вход.
func TestResumeStartsAgentOnEmptyChannel(t *testing.T) {
	root, log := runningGuest(t, liveFixture(liveEmpty, ""))
	state := os.Getenv("V2_STATE_DIR")
	if err := rememberSession(state+"/vm-01", "claude", "my-api"); err != nil {
		t.Fatal(err)
	}
	console, _, errorsOutput := quietStreams("")
	if code := execute([]string{"--root", root, "go", "vm-01"}, console); code != 0 {
		t.Fatalf("код %d, ошибки: %s", code, errorsOutput)
	}
	recorded, err := os.ReadFile(log)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(string(recorded), "target=claude\n") {
		t.Fatalf("при пустом канале atrice go вызвал:\n%s", recorded)
	}
}

// На старом образе канала нет, и про сессии неизвестно ничего: решает память
// CLI — другого источника там не появится.
func TestResumeFallsBackToMemoryOnOldImage(t *testing.T) {
	root, log := runningGuest(t, `{"api":1,"state":"old-image","hint":"Образ гостя без канала сессий.","sessions":[]}`)
	state := os.Getenv("V2_STATE_DIR")
	if err := rememberSession(state+"/vm-01", "claude", "my-api"); err != nil {
		t.Fatal(err)
	}
	console, _, errorsOutput := quietStreams("")
	if code := execute([]string{"--root", root, "go", "vm-01"}, console); code != 0 {
		t.Fatalf("код %d, ошибки: %s", code, errorsOutput)
	}
	recorded, err := os.ReadFile(log)
	if err != nil {
		t.Fatal(err)
	}
	if string(recorded) != "target=vm-console\nVM=vm-01\n" {
		t.Fatalf("на старом образе atrice go вызвал:\n%s", recorded)
	}
	// Без записи о прошлом входе на старом образе запускается агент.
	if err := os.Remove(state + "/vm-01/" + sessionFile); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(log); err != nil {
		t.Fatal(err)
	}
	channelCache.clear()
	console, _, errorsOutput = quietStreams("")
	if code := execute([]string{"--root", root, "go", "vm-01"}, console); code != 0 {
		t.Fatalf("код %d, ошибки: %s", code, errorsOutput)
	}
	recorded, err = os.ReadFile(log)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(string(recorded), "target=claude\n") {
		t.Fatalf("без памяти о входе atrice go вызвал:\n%s", recorded)
	}
}

// Занятая консоль видна по отчёту канала: вход не удастся ни одной целью, и
// отказ объясняется на хосте, а не изнутри гостя.
func TestResumeRefusesBusyConsole(t *testing.T) {
	root, log := runningGuest(t,
		`{"api":1,"state":"empty","hint":"Канал работает, живых сессий нет.","console":"busy","sessions":[]}`)
	console, _, errorsOutput := quietStreams("")
	if code := execute([]string{"--root", root, "go", "vm-01"}, console); code != 1 {
		t.Fatalf("код %d, ожидался 1", code)
	}
	if !strings.Contains(errorsOutput.String(), "Ctrl+]") {
		t.Fatalf("отказ должен подсказывать выход из консоли: %s", errorsOutput)
	}
	if _, err := os.Stat(log); err == nil {
		t.Fatal("при занятой консоли меняющие цели не запускаются")
	}
}

// Пустой список подтверждать нечего: Enter не делает ничего — ни ВМ, ни
// агента он не запускает и оболочку сам не открывает.
func TestSessionsScreenEnterNeedsSelection(t *testing.T) {
	root, log := runningGuest(t, liveFixture(liveEmpty, ""))
	quiet := streams{errors: io.Discard, output: io.Discard}
	typeKeys(t, "\r", "\n", "\x1d")
	output := &strings.Builder{}
	target, session, err := sessionsScreen(root, "vm-01", streams{output: output, errors: io.Discard}, quiet)
	if err != nil {
		t.Fatal(err)
	}
	if target != "" || session != "" {
		t.Fatalf("без выбора Enter выбрал %q %q", target, session)
	}
	if _, err := os.Stat(log); err == nil {
		t.Fatal("Enter без выбора не должен запускать цели")
	}
	if !strings.Contains(output.String(), "живых сессий нет") {
		t.Fatalf("экран должен объяснить, что подтверждать нечего: %s", output)
	}
	// На старом образе и при отказе канала — то же самое.
	for _, report := range []string{
		`{"api":1,"state":"old-image","hint":"Образ гостя без канала сессий.","sessions":[]}`,
		`{"api":1,"state":"no-service","hint":"Служба не ответила.","sessions":[]}`,
	} {
		fakeChannel(t, root, report)
		typeKeys(t, "\r", "\x1d")
		target, session, err = sessionsScreen(root, "vm-01", streams{output: io.Discard, errors: io.Discard}, quiet)
		if err != nil || target != "" || session != "" {
			t.Fatalf("отчёт %s: Enter выбрал %q %q (%v)", report, target, session, err)
		}
	}
}

// Подключение к выбранной сессии идёт по существующему протоколу: цель
// vm-session-open несёт имя сессии в RESUME.
func TestMenuTargetCarriesSessionInResume(t *testing.T) {
	root, log := fakeProject(t, 0)
	console, _, _ := quietStreams("")
	vm := VM{Name: "vm-01", StatusKnown: true, GuestStatus: "Up 5 minutes",
		Resources: &Resources{2, 4096, 12}, Proxy: true}
	if !menuTarget(root, "vm-session-open", vm, "", "20261123T0915-9f3c", console) {
		t.Fatal("подключение к сессии должно удаться")
	}
	recorded, err := os.ReadFile(log)
	if err != nil {
		t.Fatal(err)
	}
	expected := "target=vm-session-open\nVM=vm-01\nRESUME=20261123T0915-9f3c\n"
	if string(recorded) != expected {
		t.Fatalf("manage.sh получил:\n%s\nожидалось:\n%s", recorded, expected)
	}
	// Возврат из сессии идёт по Ctrl+], поэтому список открывается сразу.
	if pauseAfter("vm-session-open", true) {
		t.Fatal("после удачного подключения список должен открываться сразу")
	}
	if !pauseAfter("vm-session-open", false) {
		t.Fatal("отказ подключения обязан остаться на экране")
	}
	// Обычная консоль осталась отдельным пунктом и RESUME не несёт.
	if err := os.Remove(log); err != nil {
		t.Fatal(err)
	}
	if !menuTarget(root, "vm-console", vm, "", "", console) {
		t.Fatal("консоль должна открываться")
	}
	recorded, err = os.ReadFile(log)
	if err != nil {
		t.Fatal(err)
	}
	if string(recorded) != "target=vm-console\nVM=vm-01\n" {
		t.Fatalf("консоль вызвала:\n%s", recorded)
	}
}

// Подключение к сессии не меняет запомненного агента: в ней продолжает
// работать тот, кого там запускали.
func TestAttachKeepsRememberedAgent(t *testing.T) {
	state := t.TempDir()
	dir := filepath.Join(state, "vm-01")
	if err := os.Mkdir(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := rememberSession(dir, "claude", "my-api"); err != nil {
		t.Fatal(err)
	}
	if err := rememberSession(dir, "vm-session-open", ""); err != nil {
		t.Fatal(err)
	}
	session := readSession(dir)
	if session == nil || session.Agent != "claude" || session.Project != "my-api" {
		t.Fatalf("после подключения запомнено %+v", session)
	}
}

// Возврат в уже подключённую сессию идёт консолью: её клиент tmux работает в
// консоли гостя и оболочку не отпускает, поэтому attach в неё отправить
// нельзя. Так же выглядит повторный вход после Ctrl+] или закрытия терминала.
func TestAttachedSessionReturnsThroughConsole(t *testing.T) {
	sessions := []LiveSession{{Name: "первая", Attached: true}, {Name: "вторая"}}
	if !attached(sessions, 0) {
		t.Fatal("первая сессия подключена")
	}
	if attached(sessions, 1) {
		t.Fatal("вторая сессия отсоединена")
	}
	if attached(sessions, 5) || attached(nil, 0) {
		t.Fatal("за границей списка подключения нет")
	}
	vm := VM{Name: "vm-01", StatusKnown: true, GuestStatus: "Up 1 hour",
		Live: &Live{State: liveLive, Sessions: sessions}}
	frame := renderSessions(vm, 0, "", 80, 24, time.Now())
	if !strings.Contains(frame, sessionAttachNote) {
		t.Fatalf("экран должен объяснить возврат консолью: %s", frame)
	}
	if frame = renderSessions(vm, 1, "", 80, 24, time.Now()); strings.Contains(frame, sessionAttachNote) {
		t.Fatalf("у отсоединённой сессии объяснение лишнее: %s", frame)
	}
}

// Новая сессия рядом с живой: a на экране сессий вызывает отдельную цель, не
// несёт RESUME (имя даёт manage.sh) и запоминает агента claude. Прежняя цель
// claude отправляла команду в оболочку гостя, где уже работал клиент tmux
// живой сессии, и гость отвечал отказом — вход срывался.
func TestNewSessionTargetRunsWithoutResume(t *testing.T) {
	root, log := fakeProject(t, 0)
	state := t.TempDir()
	writeVM(t, state, "vm-01", "2 4096 12\n", "")
	t.Setenv("V2_STATE_DIR", state)
	console, _, _ := quietStreams("")
	vm := VM{Name: "vm-01", StatusKnown: true, GuestStatus: "Up 5 minutes",
		Resources: &Resources{2, 4096, 12}, Proxy: true}
	if !menuTarget(root, "vm-session-claude", vm, "", "", console) {
		t.Fatal("новая сессия должна создаваться")
	}
	recorded, err := os.ReadFile(log)
	if err != nil {
		t.Fatal(err)
	}
	if string(recorded) != "target=vm-session-claude\nVM=vm-01\n" {
		t.Fatalf("manage.sh получил:\n%s", recorded)
	}
	// Возврат из сессии идёт по Ctrl+], поэтому список открывается сразу.
	if pauseAfter("vm-session-claude", true) {
		t.Fatal("после удачного создания список должен открываться сразу")
	}
	if !pauseAfter("vm-session-claude", false) {
		t.Fatal("отказ создания обязан остаться на экране")
	}
	session := readSession(filepath.Join(state, "vm-01"))
	if session == nil || session.Agent != "claude" {
		t.Fatalf("после новой сессии запомнено %+v", session)
	}
}

// Занятая консоль гостя — запрет на новую сессию: команда создания ушла бы
// работающему в оболочке процессу как нажатия клавиш. Отказ объясняется
// строкой состояния, цель не запускается и записи о входе не появляется.
func TestNewSessionRefusalLeavesNoRecord(t *testing.T) {
	_, log := fakeProject(t, 0)
	state := t.TempDir()
	writeVM(t, state, "vm-01", "2 4096 12\n", "")
	t.Setenv("V2_STATE_DIR", state)
	busy := VM{Name: "vm-01", StatusKnown: true, GuestStatus: "Up 5 minutes",
		Resources: &Resources{2, 4096, 12}, Proxy: true,
		Live: &Live{State: liveLive, Console: "busy"}}
	trouble := newSessionTrouble(busy)
	if !strings.Contains(trouble, "консоль гостя занята") {
		t.Fatalf("отказ при занятой консоли: %q", trouble)
	}
	stopped := VM{Name: "vm-01", StatusKnown: true, GuestStatus: "Exited (0) 1 minute ago",
		Resources: &Resources{2, 4096, 12}, Proxy: true}
	if !strings.Contains(newSessionTrouble(stopped), "остановлена") {
		t.Fatalf("у остановленной отказ: %q", newSessionTrouble(stopped))
	}
	free := busy
	free.Live = &Live{State: liveLive, Console: "free"}
	if newSessionTrouble(free) != "" {
		t.Fatalf("при свободной консоли запрета быть не должно: %q", newSessionTrouble(free))
	}
	// Живая сессия сама по себе создание не запрещает: отсоединённый tmux
	// оболочку не занимает.
	detached := free
	detached.Live = &Live{State: liveLive, Console: "free",
		Sessions: []LiveSession{{Name: "claude-cbd1ff26dec69e75", Command: "claude"}}}
	if newSessionTrouble(detached) != "" {
		t.Fatalf("живая сессия не должна запрещать новую: %q", newSessionTrouble(detached))
	}
	if _, err := os.Stat(log); err == nil {
		t.Fatal("проверка запрета не должна запускать цели")
	}
	if readSession(filepath.Join(state, "vm-01")) != nil {
		t.Fatal("отказ не должен оставлять запись о входе")
	}
}

// Команда sessions перечислена в справке, и разбор обязан её знать: в чистой
// сборке она отвечала «неизвестная команда sessions», хотя установленный
// бинарник из рабочего дерева отдавал отчёт канала.
func TestSessionsCommandKeepsItsContract(t *testing.T) {
	if !strings.Contains(helpText, "sessions <VM>") {
		t.Fatal("справка больше не обещает команду sessions")
	}
	parsed, err := parseArgs([]string{"sessions", "vm-02"})
	if err != nil {
		t.Fatalf("разбор команды sessions: %v", err)
	}
	if parsed.target != "vm-sessions" || parsed.environment["VM"] != "vm-02" {
		t.Fatalf("разобрано %+v", parsed)
	}
	root, log := fakeProject(t, 0)
	console, _, errorsOutput := quietStreams("")
	if code := execute([]string{"--root", root, "sessions", "vm-02"}, console); code != 0 {
		t.Fatalf("код %d, ошибки: %s", code, errorsOutput)
	}
	recorded, err := os.ReadFile(log)
	if err != nil {
		t.Fatal(err)
	}
	if string(recorded) != "target=vm-sessions\nVM=vm-02\n" {
		t.Fatalf("manage.sh получил:\n%s", recorded)
	}
}

// Пустой список канала не означает, что в госте ничего не работает: агент,
// запущенный прямо в serial-оболочке, сессией tmux не является. Состояние
// оболочки приходит отдельной читающей целью, показывается отдельно и не
// подменяется ни пустым списком, ни отказом опроса.
func TestShellStateTellsSerialApart(t *testing.T) {
	root, log := fakeProject(t, 0)
	state := t.TempDir()
	writeVM(t, state, "vm-01", "2 4096 12\n", "")
	t.Setenv("V2_STATE_DIR", state)
	t.Setenv("V2_PROJECT_PREFIX", "стенд")
	fakeDocker(t, "стенд", "vm-01", "Up 7 minutes")
	cases := []struct {
		name     string
		report   string
		shell    string
		expected string
	}{
		{"занятая оболочка при пустом списке", liveFixture(liveEmpty, ""), shellBusy,
			"на serial работает приложение"},
		{"свободная оболочка при пустом списке", liveFixture(liveEmpty, ""), shellReady,
			"сессий нет"},
		{"отказ диагностики не выдумывает ответа", liveFixture(liveEmpty, ""), shellUnknown,
			"сессий нет"},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			fakeChannel(t, root, test.report)
			fakeShell(t, root, test.shell)
			vms := []VM{{Name: "vm-01", StatusKnown: true, GuestStatus: "Up 7 minutes",
				Resources: &Resources{2, 4096, 12}, Proxy: true}}
			applyLive(root, vms, &liveCache{}, time.Now())
			text := liveText(vms[0])
			if !strings.Contains(text, test.expected) {
				t.Fatalf("колонка показала %q, ожидалось %q", text, test.expected)
			}
			if test.shell == shellBusy {
				if !strings.Contains(liveStateLine(vms[0]), "на serial-оболочке работает приложение") {
					t.Fatalf("строка состояния: %q", liveStateLine(vms[0]))
				}
				// Запуск агента и новая сессия в занятую оболочку не идут:
				// команда ушла бы приложению как нажатия клавиш. Завершать его
				// за пользователя нельзя, поэтому отказ называет обычный вход.
				for _, trouble := range []string{claudeTrouble(vms[0]), newSessionTrouble(vms[0])} {
					if !strings.Contains(trouble, "войдите по c") {
						t.Fatalf("отказ при занятой оболочке: %q", trouble)
					}
				}
			}
			if test.shell == shellReady && claudeTrouble(vms[0]) != "" {
				t.Fatalf("свободная оболочка не должна запрещать запуск: %q", claudeTrouble(vms[0]))
			}
		})
	}
	// Читающая диагностика меняющих целей не запускает.
	if _, err := os.Stat(log); err == nil {
		t.Fatal("опрос оболочки не должен запускать меняющие цели")
	}
}

// Прямой путь в сессии независим от serial-оболочки: при занятой консоли и
// занятой оболочке a создаёт и открывает новую сессию, а Enter открывает ровно
// подсвеченную по имени. Без прямого пути остаётся прежнее поведение — путь
// через консоль с его отказом при занятости.
func TestDirectPathOpensSessionsWithBusySerial(t *testing.T) {
	root, _ := fakeProject(t, 0)
	state := t.TempDir()
	writeVM(t, state, "vm-01", "2 4096 12\n", "")
	t.Setenv("V2_STATE_DIR", state)
	t.Setenv("V2_PROJECT_PREFIX", "стенд")
	fakeDocker(t, "стенд", "vm-01", "Up 7 minutes")
	busy := `{"api":1,"state":"live","hint":"подсказка канала","console":"busy","sessions":[` +
		liveSessionFixture + `]}`
	quiet := streams{errors: io.Discard, output: io.Discard}
	cases := []struct {
		name     string
		direct   string
		keys     []string
		expected string
		session  string
	}{
		{"новая сессия прямым путём", directReady, []string{"a"}, "vm-session-new", ""},
		{"новая сессия по русской ф", directReady, []string{"ф"}, "vm-session-new", ""},
		{"подключённая сессия прямым путём", directReady, []string{"\r"},
			"vm-session-direct", "20261123T0915-9f3c"},
		{"без прямого пути — через консоль", directAbsent, []string{"a"}, "vm-session-claude", ""},
		{"без прямого пути подключённая идёт консолью", directAbsent, []string{"\r"},
			"vm-console", ""},
		{"служба не установлена — тоже через консоль", directSilent, []string{"a"},
			"vm-session-claude", ""},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			fakeChannel(t, root, busy)
			fakeShell(t, root, shellBusy)
			fakeDirect(t, root, test.direct)
			typeKeys(t, test.keys...)
			output := &strings.Builder{}
			console := streams{output: output, errors: io.Discard}
			target, session, err := sessionsScreen(root, "vm-01", console, quiet)
			if err != nil {
				t.Fatal(err)
			}
			if target != test.expected || session != test.session {
				t.Fatalf("выбрана цель %q сессии %q, ожидались %q и %q",
					target, session, test.expected, test.session)
			}
		})
	}
	// Прямому пути занятая оболочка и занятая консоль не мешают: запрета нет,
	// и отказ при них больше не выдаётся.
	direct := VM{Name: "vm-01", StatusKnown: true, GuestStatus: "Up 7 minutes",
		Resources: &Resources{2, 4096, 12}, Proxy: true,
		Live: &Live{State: liveLive, Console: "busy", Shell: shellBusy, Direct: directReady}}
	if trouble := consoleTrouble(direct); trouble != "" {
		t.Fatalf("прямой путь не должен запрещаться: %q", trouble)
	}
	if !strings.Contains(liveStateLine(direct), "прямой вход в сессии доступен") {
		t.Fatalf("строка состояния: %q", liveStateLine(direct))
	}
	// Память CLI: новая сессия помнит агента, вход в существующую его хранит.
	dir := filepath.Join(state, "vm-01")
	if err := rememberSession(dir, "vm-session-new", "my-api"); err != nil {
		t.Fatal(err)
	}
	if session := readSession(dir); session == nil || session.Agent != "claude" {
		t.Fatalf("после новой сессии запомнено %+v", readSession(dir))
	}
	if err := rememberSession(dir, "vm-session-direct", ""); err != nil {
		t.Fatal(err)
	}
	if session := readSession(dir); session == nil || session.Agent != "claude" ||
		session.Project != "my-api" {
		t.Fatalf("после входа в сессию запомнено %+v", readSession(dir))
	}
}
