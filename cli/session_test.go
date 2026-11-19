package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestSessionRoundTrip(t *testing.T) {
	state := t.TempDir()
	writeVM(t, state, "vm-01", "2 4096 12\n", "")
	dir := filepath.Join(state, "vm-01")
	if err := rememberSession(dir, "claude", "my-api"); err != nil {
		t.Fatal(err)
	}
	session := readSession(dir)
	if session == nil || session.Agent != "claude" || session.Project != "my-api" {
		t.Fatalf("прочитано %+v", session)
	}
	if time.Since(session.Opened) > time.Minute {
		t.Fatalf("время сессии %v", session.Opened)
	}
	vms, err := loadVMs(state)
	if err != nil {
		t.Fatal(err)
	}
	if vms[0].Session == nil || vms[0].Session.Project != "my-api" {
		t.Fatalf("сессия не попала в состояние гостя: %+v", vms[0].Session)
	}
}

func TestRememberSessionIgnoresUnknownTarget(t *testing.T) {
	state := t.TempDir()
	writeVM(t, state, "vm-01", "2 4096 12\n", "")
	dir := filepath.Join(state, "vm-01")
	if err := rememberSession(dir, "vm-stop", ""); err != nil {
		t.Fatal(err)
	}
	if readSession(dir) != nil {
		t.Fatal("остановка гостя не является сессией")
	}
}

func TestRememberSessionDoesNotCreateGuest(t *testing.T) {
	state := t.TempDir()
	dir := filepath.Join(state, "vm-77")
	if err := rememberSession(dir, "claude", ""); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(dir); err == nil {
		t.Fatal("запись о сессии не должна создавать каталог гостя")
	}
}

func TestLastSession(t *testing.T) {
	fresh := VM{Name: "vm-01"}
	if target := lastSession(fresh); target != "claude" {
		t.Fatalf("без сессий запускается агент, получено %q", target)
	}
	used := VM{Name: "vm-01", Session: &Session{Agent: "claude", Project: "my-api"}}
	if target := lastSession(used); target != "vm-console" {
		t.Fatalf("к работающему агенту подключаются через консоль, получено %q", target)
	}
}

func TestHumanAge(t *testing.T) {
	now := time.Date(2026, 11, 19, 12, 0, 0, 0, time.UTC)
	cases := []struct {
		opened   time.Time
		expected string
	}{
		{now.Add(-30 * time.Second), "только что"},
		{now.Add(-5 * time.Minute), "5 мин назад"},
		{now.Add(-3 * time.Hour), "3 ч назад"},
		{now.Add(-50 * time.Hour), "2 сут назад"},
		{now.Add(time.Hour), ""},
		{time.Time{}, ""},
	}
	for _, test := range cases {
		if got := humanAge(test.opened, now); got != test.expected {
			t.Fatalf("для %v получено %q, ожидалось %q", test.opened, got, test.expected)
		}
	}
}

func TestSessionText(t *testing.T) {
	now := time.Date(2026, 11, 19, 12, 0, 0, 0, time.UTC)
	if got := sessionText(VM{}, now); got != "—" {
		t.Fatalf("без сессии получено %q", got)
	}
	vm := VM{Session: &Session{Agent: "claude", Project: "my-api", Opened: now.Add(-2 * time.Hour)}}
	if got := sessionText(vm, now); got != "claude: my-api, 2 ч назад" {
		t.Fatalf("получено %q", got)
	}
	long := VM{Session: &Session{Agent: "claude", Project: strings.Repeat("каталог/", 20), Opened: now}}
	if got := sessionText(long, now); len([]rune(got)) > projectLimit+30 {
		t.Fatalf("длинный проект должен обрезаться: %q", got)
	}
}

func TestResumeKeepsAgentAndProject(t *testing.T) {
	state := t.TempDir()
	writeVM(t, state, "vm-01", "2 4096 12\n", "")
	dir := filepath.Join(state, "vm-01")
	if err := rememberSession(dir, "claude", "my-api"); err != nil {
		t.Fatal(err)
	}
	// Возврат через консоль не меняет агента: в ней продолжает работать он же.
	if err := rememberSession(dir, "vm-console", ""); err != nil {
		t.Fatal(err)
	}
	session := readSession(dir)
	if session == nil || session.Agent != "claude" || session.Project != "my-api" {
		t.Fatalf("после возврата сессия стала %+v", session)
	}
}

func TestLastSessionNeedsRunningGuest(t *testing.T) {
	stopped := VM{Name: "vm-01", StatusKnown: true, Session: &Session{Agent: "claude"}}
	if target := lastSession(stopped); target != "claude" {
		t.Fatalf("в остановленного гостя возвращаются запуском агента, получено %q", target)
	}
	running := VM{Name: "vm-01", StatusKnown: true, GuestStatus: "Up 2 hours",
		Session: &Session{Agent: "claude"}}
	if target := lastSession(running); target != "vm-console" {
		t.Fatalf("к работающему агенту подключаются через консоль, получено %q", target)
	}
}

func TestSessionTextDropsControlCharacters(t *testing.T) {
	state := t.TempDir()
	writeVM(t, state, "vm-01", "2 4096 12\n", "")
	dir := filepath.Join(state, "vm-01")
	if err := rememberSession(dir, "claude", "api\x1b[2Jx\nY"); err != nil {
		t.Fatal(err)
	}
	session := readSession(dir)
	if session == nil {
		t.Fatal("сессия не прочиталась")
	}
	for _, symbol := range session.Project {
		if control(symbol) {
			t.Fatalf("управляющий символ остался в проекте: %q", session.Project)
		}
	}
	vm := VM{Session: session}
	if strings.ContainsRune(sessionText(vm, time.Now()), 27) {
		t.Fatalf("строка списка содержит escape: %q", sessionText(vm, time.Now()))
	}
}
