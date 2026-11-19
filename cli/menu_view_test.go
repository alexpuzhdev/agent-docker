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
	cases := map[byte]menuAction{
		'q': actionQuit, 3: actionQuit, 4: actionQuit,
		'k': actionUp, 'A': actionUp,
		'j': actionDown, 'B': actionDown,
		'r': actionRefresh, 'n': actionCreate,
		'c': actionNone, 'z': actionNone,
	}
	for key, expected := range cases {
		if got := navigation(key); got != expected {
			t.Fatalf("клавиша %q дала %q, ожидалось %q", key, got, expected)
		}
	}
}

func TestKeyActionsCoverManageTargets(t *testing.T) {
	expected := map[byte]string{
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

func TestTruncate(t *testing.T) {
	if got := truncate("короткая", 20); got != "короткая" {
		t.Fatalf("получено %q", got)
	}
	got := truncate("очень длинная строка", 10)
	if utf8.RuneCountInString(got) != 10 || !strings.HasSuffix(got, "…") {
		t.Fatalf("получено %q", got)
	}
}
