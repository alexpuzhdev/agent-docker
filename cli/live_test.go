package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// liveFixture — отчёт канала для теста.
func liveFixture(state string, sessions string) string {
	report := `{"api":1,"state":"` + state + `","hint":"подсказка канала","console":"free","sessions":[` +
		sessions + `]}`
	return report
}

const liveSessionFixture = `{"name":"20261123T0915-9f3c","created":1764000000,` +
	`"attached":true,"command":"claude"}`

// liveTwoSessions — две сессии: по такому списку проверяется курсор и то, что
// Enter подтверждает именно подсвеченную сессию.
const liveTwoSessions = liveSessionFixture + `,{"name":"20261122T2030-11ab",` +
	`"created":1763900000,"attached":false,"command":"bash"}`

func TestParseLiveReadsReport(t *testing.T) {
	report, err := parseLive([]byte(liveFixture(liveLive, liveSessionFixture) + "\n"))
	if err != nil {
		t.Fatal(err)
	}
	if report.State != liveLive || len(report.Sessions) != 1 {
		t.Fatalf("получено %+v", report)
	}
	session := report.Sessions[0]
	if session.Name != "20261123T0915-9f3c" || session.Command != "claude" || !session.Attached {
		t.Fatalf("сессия разобрана как %+v", session)
	}
	if report.Hint != "подсказка канала" || report.Console != "free" {
		t.Fatalf("подсказка и консоль разобраны как %q и %q", report.Hint, report.Console)
	}
}

// Перед отчётом docker compose печатает своё, поэтому берётся последняя
// непустая строка вывода.
func TestParseLiveTakesLastLine(t *testing.T) {
	output := "service \"vm\" is running\n\n" + liveFixture(liveEmpty, "") + "\n\n"
	report, err := parseLive([]byte(output))
	if err != nil {
		t.Fatal(err)
	}
	if report.State != liveEmpty || len(report.Sessions) != 0 {
		t.Fatalf("получено %+v", report)
	}
}

func TestParseLiveRejectsUnusableReports(t *testing.T) {
	cases := map[string]string{
		"пустой вывод":        "",
		"не JSON":             "сессий нет\n",
		"чужая версия":        `{"api":2,"state":"live","sessions":[]}`,
		"без состояния":       `{"api":1,"sessions":[]}`,
		"состояние пробелами": `{"api":1,"state":"   ","sessions":[]}`,
	}
	for name, output := range cases {
		if _, err := parseLive([]byte(output)); err == nil {
			t.Fatalf("%s: отчёт должен быть отклонён", name)
		}
	}
}

// Отчёт приходит от недоверенного гостя: управляющие символы из него не
// должны попадать в терминал и ломать разметку списка.
func TestParseLiveCleansGuestText(t *testing.T) {
	output := `{"api":1,"state":"live","hint":"стро\u001b[2Jка","sessions":` +
		`[{"name":"сес\u0007сия","created":1,"attached":false,"command":"ba\u001bsh"}]}`
	report, err := parseLive([]byte(output))
	if err != nil {
		t.Fatal(err)
	}
	for _, value := range []string{report.Hint, report.Sessions[0].Name, report.Sessions[0].Command} {
		if strings.ContainsAny(value, "\x1b\x07") {
			t.Fatalf("управляющий символ остался в %q", value)
		}
	}
}

// Состояние расширяемо в рамках api = 1: незнакомое показывается как есть и
// не превращается ни в пустой список, ни в отказ разбора.
func TestLiveTextKeepsUnknownState(t *testing.T) {
	report, err := parseLive([]byte(`{"api":1,"state":"что-то новое","sessions":[]}`))
	if err != nil {
		t.Fatal(err)
	}
	vm := VM{Name: "vm-01", StatusKnown: true, GuestStatus: "Up 1 minute", Live: report}
	if got := liveText(vm); got != "что-то новое" {
		t.Fatalf("получено %q", got)
	}
}

// Колонка сессий различает старый образ, пустой список и отказ канала: ни
// одно состояние не выглядит как другое.
func TestLiveTextDistinguishesStates(t *testing.T) {
	running := VM{Name: "vm-01", StatusKnown: true, GuestStatus: "Up 1 minute",
		Resources: &Resources{2, 4096, 12}, Proxy: true}
	cases := []struct {
		state    string
		expected string
	}{
		{liveEmpty, "сессий нет"},
		{liveOldImage, "старый образ"},
		{liveNoTmux, "нет tmux"},
		{liveNoService, "служба не ответила"},
		{liveNoPermission, "нет доступа к tmux"},
		{liveUnparsed, "формат не разобран"},
		{liveSlow, "канал не успел"},
		{liveBroken, "ответ испорчен"},
		{liveNoReport, "опрос не удался"},
	}
	seen := make(map[string]string)
	for _, test := range cases {
		vm := running
		vm.Live = &Live{State: test.state}
		got := liveText(vm)
		if got != test.expected {
			t.Fatalf("состояние %s показано как %q, ожидалось %q", test.state, got, test.expected)
		}
		if previous, busy := seen[got]; busy {
			t.Fatalf("состояния %s и %s показаны одинаково: %q", previous, test.state, got)
		}
		seen[got] = test.state
	}
	// Живые сессии перечисляются и считаются.
	vm := running
	vm.Live = &Live{State: liveLive, Sessions: []LiveSession{
		{Name: "первая", Command: "claude", Attached: true},
		{Name: "вторая", Command: "bash"},
		{Name: "третья", Command: "tmux"},
	}}
	got := liveText(vm)
	for _, expected := range []string{"3 сессии", "claude*", "bash", "+1"} {
		if !strings.Contains(got, expected) {
			t.Fatalf("в колонке нет %q: %q", expected, got)
		}
	}
	// Состояние live с пустым списком — это тоже «сессий нет».
	vm.Live = &Live{State: liveLive}
	if got := liveText(vm); got != "сессий нет" {
		t.Fatalf("получено %q", got)
	}
	// Пропущенные строки tmux и занятая консоль — отдельные отметки.
	vm.Live = &Live{State: liveLive, Skipped: 2, Console: "busy",
		Sessions: []LiveSession{{Name: "одна", Command: "claude"}}}
	got = liveText(vm)
	for _, expected := range []string{"1 сессия", "пропущено 2", "консоль занята"} {
		if !strings.Contains(got, expected) {
			t.Fatalf("в колонке нет %q: %q", expected, got)
		}
	}
}

// Канал спрашивают только у запущенного гостя, поэтому у остальных колонка
// говорит о состоянии гостя, а не о сессиях.
func TestLiveTextWithoutReport(t *testing.T) {
	cases := []struct {
		vm       VM
		expected string
	}{
		{VM{}, "неизвестно"},
		{VM{StatusKnown: true}, "не запущена"},
		{VM{StatusKnown: true, GuestStatus: "Up 1 minute"}, "не опрошен"},
	}
	for _, test := range cases {
		if got := liveText(test.vm); got != test.expected {
			t.Fatalf("получено %q, ожидалось %q", got, test.expected)
		}
	}
}

// Память CLI о прошлом входе в колонку живых сессий не попадает: запись о
// давнем входе выглядела бы живой сессией.
func TestListColumnTakesSessionsOnlyFromChannel(t *testing.T) {
	vm := VM{Name: "vm-01", StatusKnown: true, GuestStatus: "Up 1 hour",
		Resources: &Resources{2, 4096, 12}, Proxy: true,
		Session: &Session{Agent: "claude", Project: "my-api", Opened: time.Now()},
		Live:    &Live{State: liveEmpty, Hint: "Канал работает, живых сессий нет."}}
	text := renderList([]VM{vm})
	if !strings.Contains(text, "СЕССИИ") || !strings.Contains(text, "сессий нет") {
		t.Fatalf("в списке нет колонки живых сессий: %s", text)
	}
	if strings.Contains(text, "my-api") {
		t.Fatalf("память о прошлом входе попала в колонку живых сессий: %s", text)
	}
	// Подробное состояние показывает и то, и другое, но называет их разным.
	status := renderStatus(vm)
	for _, expected := range []string{"Живые сессии: сессий нет", "Последний вход из CLI", "my-api"} {
		if !strings.Contains(status, expected) {
			t.Fatalf("в подробном состоянии нет %q: %s", expected, status)
		}
	}
}

func TestSessionCountDeclension(t *testing.T) {
	cases := map[int]string{1: "1 сессия", 2: "2 сессии", 4: "4 сессии", 5: "5 сессий",
		11: "11 сессий", 14: "14 сессий", 21: "21 сессия", 112: "112 сессий"}
	for count, expected := range cases {
		if got := sessionCount(count); got != expected {
			t.Fatalf("получено %q, ожидалось %q", got, expected)
		}
	}
}

func TestLiveCacheKeepsReportForTTL(t *testing.T) {
	cache := &liveCache{}
	now := time.Now()
	cache.remember("/root", "vm-01", &Live{State: liveEmpty}, now)
	if _, known := cache.lookup("/root", "vm-01", now.Add(liveTTL/2)); !known {
		t.Fatal("свежий отчёт должен находиться")
	}
	if _, known := cache.lookup("/root", "vm-01", now.Add(liveTTL)); known {
		t.Fatal("устаревший отчёт должен опрашиваться заново")
	}
	if _, known := cache.lookup("/другой", "vm-01", now); known {
		t.Fatal("отчёты разных каталогов не должны смешиваться")
	}
	if _, known := cache.lookup("/root", "vm-02", now); known {
		t.Fatal("отчёты разных гостей не должны смешиваться")
	}
	cache.clear()
	if _, known := cache.lookup("/root", "vm-01", now); known {
		t.Fatal("обновление должно забывать прежний опрос")
	}
}

// Опрашивается только запущенный гость: у остановленного живых сессий нет по
// определению, а отказ опроса не выглядит как пустой список.
func TestApplyLiveQueriesRunningGuestsOnly(t *testing.T) {
	root, _ := fakeProject(t, 0)
	fakeChannel(t, root, liveFixture(liveLive, liveSessionFixture))
	vms := []VM{
		{Name: "vm-01", StatusKnown: true, GuestStatus: "Up 2 minutes"},
		{Name: "vm-02", StatusKnown: true, GuestStatus: "Exited (0) 1 minute ago"},
	}
	applyLive(root, vms, &liveCache{}, time.Now())
	if vms[0].Live == nil || vms[0].Live.State != liveLive {
		t.Fatalf("у запущенного гостя отчёт %+v", vms[0].Live)
	}
	if vms[1].Live != nil {
		t.Fatalf("остановленного гостя опрашивать не нужно: %+v", vms[1].Live)
	}
}

// Отказ опроса — отдельное состояние: про сессии в этом случае неизвестно
// ничего, и считать его пустым списком нельзя.
func TestApplyLiveReportsProbeFailure(t *testing.T) {
	root, _ := fakeProject(t, 0)
	script := "#!/bin/bash\necho 'vm-01 остановлена: диагностика канала ей не нужна.' >&2\nexit 1\n"
	if err := os.WriteFile(filepath.Join(root, marker), []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	vms := []VM{{Name: "vm-01", StatusKnown: true, GuestStatus: "Up 2 minutes"}}
	applyLive(root, vms, &liveCache{}, time.Now())
	if vms[0].Live == nil || vms[0].Live.State != liveNoReport {
		t.Fatalf("получено %+v", vms[0].Live)
	}
	if !strings.Contains(vms[0].Live.Detail, "диагностика канала") {
		t.Fatalf("причина отказа потеряна: %q", vms[0].Live.Detail)
	}
	if liveText(vms[0]) == "сессий нет" {
		t.Fatal("отказ опроса не должен выглядеть как пустой список")
	}
}

// Экран живых сессий различает те же состояния, что и колонка, и показывает
// подсказку канала как есть.
func TestRenderSessionsShowsChannelStates(t *testing.T) {
	running := VM{Name: "vm-02", StatusKnown: true, GuestStatus: "Up 1 hour",
		Resources: &Resources{2, 4096, 12}, Proxy: true}
	live := running
	live.Live = &Live{State: liveLive, Hint: "Канал работает, список сессий получен.",
		Sessions: []LiveSession{{Name: "20261123T0915-9f3c", Created: 1764000000,
			Attached: true, Command: "claude"}}}
	frame := renderSessions(live, 0, "", 80, 24, time.Unix(1764003600, 0).UTC())
	for _, expected := range []string{"vm-02", "live", "список сессий получен",
		"20261123T0915-9f3c", "claude", "1 ч назад"} {
		if !strings.Contains(frame, expected) {
			t.Fatalf("на экране сессий нет %q: %s", expected, frame)
		}
	}
	old := running
	old.Live = &Live{State: liveOldImage, Hint: "Образ гостя без канала сессий."}
	frame = renderSessions(old, 0, "", 80, 24, time.Now())
	for _, expected := range []string{"old-image", "старый образ", "без канала сессий"} {
		if !strings.Contains(frame, expected) {
			t.Fatalf("старый образ должен быть назван: %s", frame)
		}
	}
	empty := running
	empty.Live = &Live{State: liveEmpty, Hint: "Канал работает, живых сессий нет."}
	frame = renderSessions(empty, 0, "", 80, 24, time.Now())
	if !strings.Contains(frame, "сессий нет") || strings.Contains(frame, "старый образ") {
		t.Fatalf("пустой список должен отличаться от старого образа: %s", frame)
	}
	broken := running
	broken.Live = &Live{State: liveNoService, Hint: "Служба не ответила.",
		Detail: "journalctl -u atrice-sessions"}
	frame = renderSessions(broken, 0, "", 80, 24, time.Now())
	for _, expected := range []string{"no-service", "служба не ответила", "journalctl"} {
		if !strings.Contains(frame, expected) {
			t.Fatalf("ошибка канала должна быть названа: %s", frame)
		}
	}
	stopped := running
	stopped.GuestStatus = "Exited (0) 1 minute ago"
	frame = renderSessions(stopped, 0, "", 80, 24, time.Now())
	if !strings.Contains(frame, "остановлена") {
		t.Fatalf("у остановленного гостя канал не спрашивают: %s", frame)
	}
}

// Кадр экрана сессий не должен выходить за окно: прокрутка сдвинула бы
// состояние канала за край.
func TestRenderSessionsFitsTerminal(t *testing.T) {
	vm := VM{Name: "vm-02", StatusKnown: true, GuestStatus: "Up 1 hour",
		Live: &Live{State: liveLive, Hint: strings.Repeat("подсказка ", 20)}}
	for index := 0; index < 30; index++ {
		vm.Live.Sessions = append(vm.Live.Sessions,
			LiveSession{Name: "сессия", Created: 1, Command: "claude"})
	}
	frame := renderSessions(vm, 0, "", 40, 12, time.Now())
	lines := strings.Split(frame, "\r\n")
	if len(lines) > 12 {
		t.Fatalf("кадр из %d строк не помещается в окно 12", len(lines))
	}
	for _, line := range lines {
		if visibleWidth(line) > 40 {
			t.Fatalf("строка шире окна: %q", line)
		}
	}
}

// Запуск агента запрещается только по тому, что хост знает точно. Живые
// сессии ему не помеха: отсоединённый tmux консоль не занимает.
func TestClaudeTroubleLooksAtConsoleOnly(t *testing.T) {
	running := VM{Name: "vm-01", StatusKnown: true, GuestStatus: "Up 1 hour",
		Resources: &Resources{2, 4096, 12}, Proxy: true}
	if trouble := claudeTrouble(running); trouble != "" {
		t.Fatalf("без отчёта канала запуск разрешён, получено %q", trouble)
	}
	withSessions := running
	withSessions.Live = &Live{State: liveLive, Console: "free",
		Sessions: []LiveSession{{Name: "одна", Command: "claude"}}}
	if trouble := claudeTrouble(withSessions); trouble != "" {
		t.Fatalf("живые сессии запуску не мешают, получено %q", trouble)
	}
	busy := running
	busy.Live = &Live{State: liveLive, Console: "busy"}
	if trouble := claudeTrouble(busy); !strings.Contains(trouble, "консоль") {
		t.Fatalf("занятая консоль должна быть названа: %q", trouble)
	}
	fresh := VM{Name: "vm-03"}
	if trouble := claudeTrouble(fresh); !strings.Contains(trouble, "не настроена") {
		t.Fatalf("ненастроенная VM: %q", trouble)
	}
}
