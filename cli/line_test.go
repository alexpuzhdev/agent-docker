package main

import (
	"strings"
	"testing"
)

// typeText прогоняет строку через редактор как последовательность нажатий.
func typeText(state line, text string) line {
	for _, symbol := range strings.Split(text, "") {
		state, _ = state.apply([]byte(symbol))
	}
	return state
}

func TestLineInsertsAndDeletes(t *testing.T) {
	state := typeText(line{}, "/go vm-01")
	if state.text() != "/go vm-01" {
		t.Fatalf("получено %q", state.text())
	}
	state, _ = state.apply([]byte{127})
	if state.text() != "/go vm-0" {
		t.Fatalf("после забоя %q", state.text())
	}
	state = typeText(state, "2")
	if state.text() != "/go vm-02" || state.cursor != 9 {
		t.Fatalf("получено %q, курсор %d", state.text(), state.cursor)
	}
}

func TestLineHandlesCyrillic(t *testing.T) {
	state := typeText(line{}, "проект")
	if state.text() != "проект" {
		t.Fatalf("получено %q", state.text())
	}
	if state.cursor != 6 {
		t.Fatalf("курсор считается в символах, получено %d", state.cursor)
	}
	state, _ = state.apply([]byte{127})
	if state.text() != "проек" {
		t.Fatalf("забой должен убирать символ целиком, получено %q", state.text())
	}
}

func TestLineCursorMovement(t *testing.T) {
	state := typeText(line{}, "abc")
	left := []byte{27, '[', 'D'}
	state, _ = state.apply(left)
	state, _ = state.apply(left)
	state = typeText(state, "X")
	if state.text() != "aXbc" {
		t.Fatalf("вставка в позицию курсора дала %q", state.text())
	}
	state, _ = state.apply([]byte{27, '[', 'C'})
	state, _ = state.apply([]byte{27, '[', '3'})
	if state.text() != "aXb" {
		t.Fatalf("удаление под курсором дало %q", state.text())
	}
	state, _ = state.apply([]byte{1})
	if state.cursor != 0 {
		t.Fatal("Ctrl+A ставит курсор в начало")
	}
	state, _ = state.apply([]byte{5})
	if state.cursor != len(state.runes) {
		t.Fatal("Ctrl+E ставит курсор в конец")
	}
}

func TestLineCutting(t *testing.T) {
	state := typeText(line{}, "/logs vm-02")
	state, _ = state.apply([]byte{23})
	if state.text() != "/logs " {
		t.Fatalf("Ctrl+W дал %q", state.text())
	}
	state, _ = state.apply([]byte{21})
	if state.text() != "" {
		t.Fatalf("Ctrl+U дал %q", state.text())
	}
	state = typeText(state, "/ls vm")
	state, _ = state.apply([]byte{1})
	state, _ = state.apply([]byte{11})
	if state.text() != "" {
		t.Fatalf("Ctrl+K от начала дал %q", state.text())
	}
}

func TestLineEvents(t *testing.T) {
	state := typeText(line{}, "/ls")
	if _, event := state.apply([]byte{13}); event != eventSubmit {
		t.Fatalf("Enter дал %q", event)
	}
	if _, event := state.apply([]byte{9}); event != eventComplete {
		t.Fatalf("Tab дал %q", event)
	}
	cleared, event := state.apply([]byte{3})
	if event != eventInterrupt || cleared.text() != "" {
		t.Fatalf("Ctrl+C дал %q и строку %q", event, cleared.text())
	}
	if _, event := state.apply([]byte{4}); event != eventNone {
		t.Fatalf("Ctrl+D при непустой строке дал %q", event)
	}
	if _, event := (line{}).apply([]byte{4}); event != eventQuit {
		t.Fatal("Ctrl+D на пустой строке завершает работу")
	}
}

func TestLineHistory(t *testing.T) {
	state := line{}
	state = state.remember("/ls")
	state = state.remember("/go vm-01")
	state = state.remember("/go vm-01")
	if len(state.history) != 2 {
		t.Fatalf("повтор не должен дублироваться: %v", state.history)
	}
	state = typeText(state, "чер")
	up := []byte{27, '[', 'A'}
	state, _ = state.apply(up)
	if state.text() != "/go vm-01" {
		t.Fatalf("первый шаг назад дал %q", state.text())
	}
	state, _ = state.apply(up)
	if state.text() != "/ls" {
		t.Fatalf("второй шаг назад дал %q", state.text())
	}
	state, _ = state.apply(up)
	if state.text() != "/ls" {
		t.Fatalf("за начало истории выходить нельзя: %q", state.text())
	}
	down := []byte{27, '[', 'B'}
	state, _ = state.apply(down)
	state, _ = state.apply(down)
	if state.text() != "чер" {
		t.Fatalf("начатая строка должна вернуться, получено %q", state.text())
	}
}

func TestRenderLinePlacesCursor(t *testing.T) {
	state := typeText(line{}, "/go vm-01")
	state, _ = state.apply([]byte{27, '[', 'D'})
	frame := renderLine("> ", state, 80)
	if !strings.HasPrefix(frame, "\r\x1b[K> /go vm-01") {
		t.Fatalf("кадр строки: %q", frame)
	}
	if !strings.HasSuffix(frame, "\x1b[1D") {
		t.Fatalf("курсор должен сдвигаться влево на один символ: %q", frame)
	}
	full := renderLine("> ", typeText(line{}, "/ls"), 80)
	if strings.Contains(full, "D") {
		t.Fatalf("курсор в конце строки не требует сдвига: %q", full)
	}
}

func TestRenderLineScrollsAndCountsVisibleWidth(t *testing.T) {
	withColors(t)
	prompt := colors.accent("> ")
	state := typeText(line{}, strings.Repeat("a", 40))
	frame := renderLine(prompt, state, 20)
	visible := strings.TrimPrefix(stripEscapes(frame), "\r")
	if len([]rune(visible)) > 20 {
		t.Fatalf("кадр шире окна: %q", visible)
	}
	if !strings.HasPrefix(visible, "> ") {
		t.Fatalf("приглашение потерялось: %q", visible)
	}
	// Курсор в конце строки: сдвигать его влево не нужно.
	if strings.Contains(frame, "D") {
		t.Fatalf("лишний сдвиг курсора: %q", frame)
	}
	// Курсор в начале: видна голова строки, сдвиг равен её длине.
	state, _ = state.apply([]byte{1})
	frame = renderLine(prompt, state, 20)
	if !strings.HasSuffix(frame, "\x1b[18D") {
		t.Fatalf("сдвиг курсора к началу: %q", frame)
	}
	visible = strings.TrimPrefix(stripEscapes(frame), "\r")
	if len([]rune(visible)) > 20 {
		t.Fatalf("кадр шире окна: %q", visible)
	}
}

func TestLinePasteWithNewlineSubmits(t *testing.T) {
	state, event := line{}.apply([]byte("/ls\n"))
	if event != eventSubmit {
		t.Fatalf("вставка с переводом строки должна выполняться, получено %q", event)
	}
	if state.text() != "/ls" {
		t.Fatalf("в строке осталось %q", state.text())
	}
	state, event = line{}.apply([]byte("vm-01"))
	if event != eventNone || state.text() != "vm-01" {
		t.Fatalf("обычная вставка дала %q и %q", event, state.text())
	}
}

func TestLineDropsControlCharacters(t *testing.T) {
	state, _ := line{}.apply([]byte("vm\x1b[31m-01"))
	if strings.ContainsRune(state.text(), 27) {
		t.Fatalf("управляющие символы не должны попадать в строку: %q", state.text())
	}
	if !strings.HasPrefix(state.text(), "vm") {
		t.Fatalf("видимый текст должен сохраняться: %q", state.text())
	}
}
