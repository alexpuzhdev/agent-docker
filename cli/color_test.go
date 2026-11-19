package main

import (
	"strings"
	"testing"
)

// withColors включает палитру на время теста.
func withColors(t *testing.T) {
	t.Helper()
	previous := colors
	colors = palette{mode: colorTrue}
	t.Cleanup(func() { colors = previous })
}

// stripEscapes убирает управляющие последовательности, оставляя видимый текст.
func stripEscapes(text string) string {
	var builder strings.Builder
	escape := false
	for _, symbol := range text {
		switch {
		case escape:
			if (symbol >= 'a' && symbol <= 'z') || (symbol >= 'A' && symbol <= 'Z') {
				escape = false
			}
		case symbol == 27:
			escape = true
		default:
			builder.WriteRune(symbol)
		}
	}
	return builder.String()
}

func TestPaletteDisabledByDefault(t *testing.T) {
	if colors.accent("vm-01") != "vm-01" {
		t.Fatal("без включённой палитры текст не меняется")
	}
	t.Setenv("NO_COLOR", "1")
	t.Setenv("TERM", "xterm-256color")
	if newPalette().enabled() {
		t.Fatal("NO_COLOR запрещает раскраску")
	}
	t.Setenv("NO_COLOR", "")
	t.Setenv("TERM", "dumb")
	if newPalette().enabled() {
		t.Fatal("терминал dumb не поддерживает раскраску")
	}
}

func TestPaletteFallsBackToIndexedColors(t *testing.T) {
	// Терминал без поддержки 24 бит получает ближайший цвет из 256.
	indexed := palette{mode: colorIndexed}
	if got := indexed.accent("vm-01"); got != "\x1b[38;5;173mvm-01\x1b[0m" {
		t.Fatalf("получено %q", got)
	}
	if got := indexed.good("работает"); got != "\x1b[38;5;72mработает\x1b[0m" {
		t.Fatalf("получено %q", got)
	}
}

func TestPaintWrapsAndResets(t *testing.T) {
	withColors(t)
	painted := colors.good("работает")
	if painted != "\x1b[38;2;95;169;127mработает\x1b[0m" {
		t.Fatalf("получено %q", painted)
	}
	if accent := colors.accent("vm-01"); accent != "\x1b[38;2;224;141;60mvm-01\x1b[0m" {
		t.Fatalf("получено %q", accent)
	}
	if colors.good("") != "" {
		t.Fatal("пустая строка не красится")
	}
}

func TestVisibleWidthIgnoresEscapes(t *testing.T) {
	withColors(t)
	if got := visibleWidth(colors.accent("vm-01")); got != 5 {
		t.Fatalf("ширина %d, ожидалось 5", got)
	}
	if got := visibleWidth("работает"); got != 8 {
		t.Fatalf("ширина %d, ожидалось 8", got)
	}
}

func TestTruncateKeepsColorAndClosesIt(t *testing.T) {
	withColors(t)
	short := truncate(colors.accent("vm-01"), 10)
	if short != colors.accent("vm-01") {
		t.Fatalf("короткая строка не меняется: %q", short)
	}
	long := truncate(colors.muted("очень длинная подсказка"), 10)
	if visibleWidth(long) != 10 {
		t.Fatalf("видимая ширина %d: %q", visibleWidth(long), long)
	}
	if !strings.HasSuffix(long, "\x1b[0m") {
		t.Fatalf("обрезанная строка должна закрывать цвет: %q", long)
	}
	if !strings.Contains(stripEscapes(long), "…") {
		t.Fatalf("обрезка отмечается многоточием: %q", long)
	}
}

func TestTableStaysAlignedWithColors(t *testing.T) {
	withColors(t)
	vms := []VM{
		{Name: "vm-01", StatusKnown: true, Proxy: true, GuestStatus: "Up 1 hour",
			Resources: &Resources{2, 4096, 12}},
		{Name: "vm-002", StatusKnown: true, Resources: &Resources{4, 8192, 40}},
	}
	table := renderList(vms)
	if !strings.Contains(table, "\x1b[") {
		t.Fatal("при включённой палитре таблица должна содержать цвет")
	}
	positions := make(map[int]bool)
	for _, line := range strings.Split(strings.TrimRight(table, "\n"), "\n") {
		positions[secondColumn(stripEscapes(line))] = true
	}
	if len(positions) != 1 {
		t.Fatalf("колонки разъехались из-за раскраски: %v", positions)
	}
}
