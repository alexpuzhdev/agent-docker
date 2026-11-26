package main

import (
	"strings"
	"testing"
	"unicode/utf8"
)

func TestRenderTableAlignsCyrillic(t *testing.T) {
	table := renderTable([]string{"VM", "СОСТОЯНИЕ"}, [][]string{
		{"vm-01", "работает"},
		{"vm-002", "остановлена"},
	})
	lines := strings.Split(strings.TrimRight(table, "\n"), "\n")
	if len(lines) != 3 {
		t.Fatalf("ожидались три строки, получено %d", len(lines))
	}
	expected := utf8.RuneCountInString("vm-002") + 2
	for index, line := range lines {
		if got := secondColumn(line); got != expected {
			t.Fatalf("строка %d начинает вторую колонку на позиции %d, ожидалось %d", index, got, expected)
		}
	}
}

// secondColumn возвращает позицию второй колонки в символах.
func secondColumn(line string) int {
	runes := []rune(line)
	index := 0
	for index < len(runes) && runes[index] != ' ' {
		index++
	}
	for index < len(runes) && runes[index] == ' ' {
		index++
	}
	return index
}

func TestRenderTableDoesNotPadLastColumn(t *testing.T) {
	table := renderTable([]string{"A", "B"}, [][]string{{"x", "короткая"}, {"y", "длинная строка"}})
	for _, line := range strings.Split(strings.TrimRight(table, "\n"), "\n") {
		if strings.HasSuffix(line, " ") {
			t.Fatalf("строка заканчивается пробелом: %q", line)
		}
	}
}

func TestRenderListEmpty(t *testing.T) {
	if !strings.Contains(renderList(nil), "atrice new") {
		t.Fatal("пустой список должен подсказывать создание гостя")
	}
}

func TestStatusText(t *testing.T) {
	cases := []struct {
		vm       VM
		expected string
	}{
		{VM{StatusKnown: true, GuestStatus: "Up 3 days", Resources: &Resources{1, 1, 1}, Proxy: true}, "работает"},
		{VM{StatusKnown: true, GuestStatus: "Exited (0) 2 hours ago", Resources: &Resources{1, 1, 1}, Proxy: true}, "остановлена"},
		{VM{StatusKnown: true, Resources: &Resources{1, 1, 1}, Proxy: true}, "не запускалась"},
		{VM{StatusKnown: true}, "не настроена"},
		{VM{}, "неизвестно"},
	}
	for _, test := range cases {
		if got := statusText(test.vm); got != test.expected {
			t.Fatalf("состояние %q, ожидалось %q для %+v", got, test.expected, test.vm)
		}
	}
}

func TestRenderStatusShowsContainerLines(t *testing.T) {
	vm := VM{Name: "vm-02", Dir: "/state/vm-02", StatusKnown: true,
		GuestStatus: "Up 10 minutes", ProxyStatus: "Up 10 minutes",
		Resources: &Resources{VCPU: 4, RAMMB: 8192, DiskGB: 40}, Proxy: true}
	text := renderStatus(vm)
	for _, expected := range []string{"vm-02", "работает", "Up 10 minutes", "8192", "/state/vm-02"} {
		if !strings.Contains(text, expected) {
			t.Fatalf("в подробном состоянии нет %q: %s", expected, text)
		}
	}
}
