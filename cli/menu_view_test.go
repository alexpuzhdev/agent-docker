package main

import (
	"strings"
	"testing"
	"time"
	"unicode/utf8"
)

func TestMoveCursorStaysInRange(t *testing.T) {
	cases := []struct{ current, delta, count, expected int }{
		{0, -1, 3, 0},
		{0, 1, 3, 1},
		{2, 1, 3, 2},
		{5, 0, 3, 2},
		{0, 1, 0, 0},
		{1, -1, 3, 0},
	}
	for _, test := range cases {
		if got := moveCursor(test.current, test.delta, test.count); got != test.expected {
			t.Fatalf("moveCursor(%d,%d,%d)=%d, ожидалось %d",
				test.current, test.delta, test.count, got, test.expected)
		}
	}
}

func TestWindowStartKeepsCursorVisible(t *testing.T) {
	cases := []struct{ cursor, visible, count, expected int }{
		{0, 5, 3, 0},
		{0, 5, 20, 0},
		{10, 5, 20, 8},
		{19, 5, 20, 15},
		{3, 0, 20, 0},
	}
	for _, test := range cases {
		got := windowStart(test.cursor, test.visible, test.count)
		if got != test.expected {
			t.Fatalf("windowStart(%d,%d,%d)=%d, ожидалось %d",
				test.cursor, test.visible, test.count, got, test.expected)
		}
		if test.visible > 0 && test.count > 0 && (test.cursor < got || test.cursor >= got+test.visible) {
			t.Fatalf("курсор %d вне окна [%d,%d)", test.cursor, got, got+test.visible)
		}
	}
}

func TestNavigationKeys(t *testing.T) {
	cases := map[rune]menuAction{
		'q': actionQuit, keyInterrupt: actionQuit, keyEndOfStream: actionQuit,
		'k': actionUp, keyUp: actionUp,
		'j': actionDown, keyDown: actionDown,
		'r': actionRefresh, 'n': actionCreate,
		'c': actionNone, 'z': actionNone,
		// Буквы стрелок сами по себе действиями не стали: стрелка приходит
		// отдельной руной, иначе ввод A переводил бы курсор.
		'A': actionNone, 'B': actionNone,
	}
	for key, expected := range cases {
		if got := navigation(key); got != expected {
			t.Fatalf("клавиша %q дала %q, ожидалось %q", key, got, expected)
		}
	}
}

func TestKeyActionsCoverManageTargets(t *testing.T) {
	expected := map[rune]string{
		'c': "vm-console", 'a': "claude", 'u': "vm-up", 's': "vm-stop",
		'g': "vm-git-status", 'l': "vm-logs", 'p': "vm-profile", 'd': "vm-remove",
	}
	for key, target := range expected {
		if keyActions[key] != target {
			t.Fatalf("клавиша %q должна вызывать %s, вызывает %s", key, target, keyActions[key])
		}
	}
	for key := range keyActions {
		if navigation(key) != actionNone {
			t.Fatalf("клавиша %q занята навигацией и действием одновременно", key)
		}
	}
}

func TestRenderMenuFitsTerminal(t *testing.T) {
	vms := []VM{
		{Name: "vm-01", StatusKnown: true, Proxy: true, Resources: &Resources{2, 4096, 12}},
		{Name: "vm-02", StatusKnown: true, Proxy: true, GuestStatus: "Up 1 hour",
			Resources: &Resources{4, 8192, 40},
			Git:       &Git{Provider: "github", Repository: "owner/name", Write: true, Branch: "work"}},
	}
	frame := renderMenu(vms, 1, 40, 24, "/home/user/agent-docker", "сообщение", time.Now())
	lines := strings.Split(strings.TrimRight(frame, "\r\n"), "\r\n")
	for _, line := range lines {
		if utf8.RuneCountInString(line) > 40 {
			t.Fatalf("строка шире окна: %q", line)
		}
	}
	if !strings.Contains(frame, "> vm-02") {
		t.Fatalf("курсор должен стоять на vm-02: %s", frame)
	}
	if strings.Contains(frame, "> vm-01") {
		t.Fatal("курсор не должен дублироваться")
	}
	if !strings.Contains(frame, "сообщение") {
		t.Fatal("сообщение должно попадать в кадр")
	}
}

func TestRenderMenuEmptyList(t *testing.T) {
	frame := renderMenu(nil, 0, 60, 24, "/root", "", time.Now())
	if !strings.Contains(frame, "клавишей n") {
		t.Fatalf("пустой список должен подсказывать создание: %s", frame)
	}
}

func TestRenderMenuScrollsLongList(t *testing.T) {
	var vms []VM
	for index := 1; index <= 40; index++ {
		vms = append(vms, VM{Name: "vm-" + string(rune('0'+index/10)) + string(rune('0'+index%10)),
			StatusKnown: true, Proxy: true, Resources: &Resources{2, 4096, 12}})
	}
	frame := renderMenu(vms, 39, 80, 20, "/root", "", time.Now())
	lines := strings.Split(strings.TrimRight(frame, "\r\n"), "\r\n")
	if len(lines) > 20 {
		t.Fatalf("кадр из %d строк не помещается в окно 20", len(lines))
	}
	if !strings.Contains(frame, "> "+vms[39].Name) {
		t.Fatalf("выбранный гость должен быть виден: %s", frame)
	}
}

func TestSelectVMKeepsGuestByName(t *testing.T) {
	vms := []VM{{Name: "vm-01"}, {Name: "vm-02"}, {Name: "vm-03"}}
	if got := selectVM(vms, "vm-02", 0); got != 1 {
		t.Fatalf("выбранный гость оказался на строке %d", got)
	}
	// Перед выбранным гостем появился новый: выбор идёт за именем, а не за
	// номером строки, иначе возврат из консоли открывал бы другого гостя.
	grown := []VM{{Name: "vm-00"}, {Name: "vm-01"}, {Name: "vm-02"}, {Name: "vm-03"}}
	if got := selectVM(grown, "vm-02", 1); got != 2 {
		t.Fatalf("после пополнения списка выбор оказался на строке %d", got)
	}
	// Выбранного гостя удалили: курсор прижимается к границам нового списка.
	if got := selectVM(vms[:1], "vm-02", 1); got != 0 {
		t.Fatalf("после удаления выбор оказался на строке %d", got)
	}
	if got := selectVM(nil, "vm-02", 1); got != 0 {
		t.Fatalf("в пустом списке выбор равен %d", got)
	}
}

func TestSelectedName(t *testing.T) {
	vms := []VM{{Name: "vm-01"}, {Name: "vm-02"}}
	if got := selectedName(vms, 1); got != "vm-02" {
		t.Fatalf("получено %q", got)
	}
	if got := selectedName(vms, 5); got != "" {
		t.Fatalf("за границей списка имени нет, получено %q", got)
	}
	if got := selectedName(nil, 0); got != "" {
		t.Fatalf("в пустом списке имени нет, получено %q", got)
	}
}

// Enter всегда открывает сессию, а выходят из неё по Ctrl+]. Такие цели не
// должны требовать ещё одного Enter: иначе возврат к списку перестал бы быть
// мгновенным и перейти к следующему гостю сразу не получилось бы.
func TestEnterTargetsReturnToListAtOnce(t *testing.T) {
	running := VM{Name: "vm-01", GuestStatus: "Up 1 minute", Session: &Session{Agent: "claude"}}
	fresh := VM{Name: "vm-02"}
	for _, vm := range []VM{running, fresh} {
		if target := lastSession(vm); !sessionTargets[target] {
			t.Fatalf("после цели %q список должен открываться сразу", target)
		}
	}
	// Читающие и необратимые цели, наоборот, оставляют вывод на экране.
	for _, target := range []string{"vm-logs", "vm-profile", "vm-git-status", "vm-remove"} {
		if sessionTargets[target] {
			t.Fatalf("вывод цели %q нужно успеть прочитать", target)
		}
	}
}

// Обычная консоль должна открываться и на старом образе, поэтому проверка
// смотрит только на настройку и запуск гостя.
func TestConsoleTroubleDistinguishesStates(t *testing.T) {
	fresh := VM{Name: "vm-01"}
	if trouble := consoleTrouble(fresh); !strings.Contains(trouble, "не настроена") {
		t.Fatalf("ненастроенная VM: %q", trouble)
	}
	configured := VM{Name: "vm-01", Resources: &Resources{2, 4096, 12}, Proxy: true}
	stopped := configured
	stopped.StatusKnown = true
	stopped.GuestStatus = "Exited (0) 1 minute ago"
	if trouble := consoleTrouble(stopped); !strings.Contains(trouble, "остановлена") {
		t.Fatalf("остановленная VM: %q", trouble)
	}
	running := configured
	running.StatusKnown = true
	running.GuestStatus = "Up 2 minutes"
	if trouble := consoleTrouble(running); trouble != "" {
		t.Fatalf("у работающей VM препятствий нет, получено %q", trouble)
	}
	// Ни сессии, ни агента у гостя нет: для обычной консоли это не помеха,
	// иначе на старом образе она перестала бы открываться.
	if running.Session != nil {
		t.Fatal("фикстура не должна содержать сессию")
	}
	// Состояние контейнеров неизвестно: запрещать попытку нельзя, отказ
	// придёт от manage.sh.
	unknown := configured
	if trouble := consoleTrouble(unknown); trouble != "" {
		t.Fatalf("при неизвестном состоянии попытка разрешена, получено %q", trouble)
	}
}

// Удачная сессия возвращает в список сразу, а любой отказ задерживает экран:
// иначе сообщение о занятой консоли исчезло бы вместе с кадром.
func TestPauseAfterKeepsFailureVisible(t *testing.T) {
	for _, target := range []string{"vm-console", "claude"} {
		if pauseAfter(target, true) {
			t.Fatalf("удачная цель %q не должна задерживать экран", target)
		}
		if !pauseAfter(target, false) {
			t.Fatalf("отказ цели %q должен остаться на экране", target)
		}
	}
	for _, target := range []string{"vm-logs", "vm-profile", "vm-git-status", "vm-remove", ""} {
		if !pauseAfter(target, true) {
			t.Fatalf("вывод цели %q нужно успеть прочитать", target)
		}
	}
}

func TestTruncate(t *testing.T) {
	if got := truncate("короткая", 20); got != "короткая" {
		t.Fatalf("получено %q", got)
	}
	got := truncate("очень длинная строка", 10)
	if utf8.RuneCountInString(got) != 10 || !strings.HasSuffix(got, "…") {
		t.Fatalf("получено %q", got)
	}
}
