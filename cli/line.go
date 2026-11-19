package main

import (
	"strconv"
	"strings"
	"unicode/utf8"
)

// lineEvent — что редактор просит сделать после обработки клавиши.
type lineEvent string

const (
	eventNone      lineEvent = ""
	eventSubmit    lineEvent = "submit"
	eventQuit      lineEvent = "quit"
	eventComplete  lineEvent = "complete"
	eventInterrupt lineEvent = "interrupt"
)

// line — состояние строки ввода. Редактор отделён от терминала, чтобы его
// поведение проверялось тестами без настоящего tty.
type line struct {
	runes   []rune
	cursor  int
	history [][]rune
	recall  int
	draft   []rune
}

// text возвращает содержимое строки.
func (state line) text() string {
	return string(state.runes)
}

// reset очищает строку, сохраняя историю.
func (state line) reset() line {
	state.runes = nil
	state.cursor = 0
	state.recall = len(state.history)
	state.draft = nil
	return state
}

// remember кладёт строку в историю, не повторяя последнюю запись подряд.
func (state line) remember(text string) line {
	if text == "" {
		return state.reset()
	}
	if count := len(state.history); count == 0 || string(state.history[count-1]) != text {
		state.history = append(state.history, []rune(text))
	}
	return state.reset()
}

// replace подставляет готовую строку и ставит курсор в конец.
func (state line) replace(text string) line {
	state.runes = []rune(text)
	state.cursor = len(state.runes)
	return state
}

// apply обрабатывает прочитанные байты и возвращает новое состояние.
func (state line) apply(data []byte) (line, lineEvent) {
	if len(data) == 0 {
		return state, eventNone
	}
	if data[0] == 27 {
		return state.escape(data), eventNone
	}
	switch data[0] {
	case 13, 10:
		return state, eventSubmit
	case 9:
		return state, eventComplete
	case 3:
		return state.reset(), eventInterrupt
	case 4:
		if len(state.runes) == 0 {
			return state, eventQuit
		}
		return state.delete(), eventNone
	case 8, 127:
		return state.backspace(), eventNone
	case 1:
		state.cursor = 0
		return state, eventNone
	case 5:
		state.cursor = len(state.runes)
		return state, eventNone
	case 21:
		state.runes = append([]rune{}, state.runes[state.cursor:]...)
		state.cursor = 0
		return state, eventNone
	case 11:
		state.runes = append([]rune{}, state.runes[:state.cursor]...)
		return state, eventNone
	case 23:
		return state.deleteWord(), eventNone
	}
	if data[0] < 32 {
		return state, eventNone
	}
	return state.insert(string(data)), eventNone
}

// escape разбирает последовательности стрелок и клавиш перехода.
func (state line) escape(data []byte) line {
	if len(data) < 3 || data[1] != '[' {
		return state
	}
	switch data[2] {
	case 'A':
		return state.recallStep(-1)
	case 'B':
		return state.recallStep(1)
	case 'C':
		if state.cursor < len(state.runes) {
			state.cursor++
		}
	case 'D':
		if state.cursor > 0 {
			state.cursor--
		}
	case 'H':
		state.cursor = 0
	case 'F':
		state.cursor = len(state.runes)
	case '3':
		return state.delete()
	}
	return state
}

// insert добавляет введённый текст в позицию курсора.
func (state line) insert(text string) line {
	addition := []rune(text)
	updated := make([]rune, 0, len(state.runes)+len(addition))
	updated = append(updated, state.runes[:state.cursor]...)
	updated = append(updated, addition...)
	updated = append(updated, state.runes[state.cursor:]...)
	state.runes = updated
	state.cursor += len(addition)
	return state
}

// backspace удаляет символ перед курсором.
func (state line) backspace() line {
	if state.cursor == 0 {
		return state
	}
	state.runes = append(append([]rune{}, state.runes[:state.cursor-1]...), state.runes[state.cursor:]...)
	state.cursor--
	return state
}

// delete удаляет символ под курсором.
func (state line) delete() line {
	if state.cursor >= len(state.runes) {
		return state
	}
	state.runes = append(append([]rune{}, state.runes[:state.cursor]...), state.runes[state.cursor+1:]...)
	return state
}

// deleteWord удаляет слово перед курсором вместе с отступом.
func (state line) deleteWord() line {
	position := state.cursor
	for position > 0 && state.runes[position-1] == ' ' {
		position--
	}
	for position > 0 && state.runes[position-1] != ' ' {
		position--
	}
	state.runes = append(append([]rune{}, state.runes[:position]...), state.runes[state.cursor:]...)
	state.cursor = position
	return state
}

// recallStep листает историю, сохраняя начатую строку.
func (state line) recallStep(step int) line {
	if len(state.history) == 0 {
		return state
	}
	if state.recall > len(state.history) || state.recall < 0 {
		state.recall = len(state.history)
	}
	if state.recall == len(state.history) && step < 0 {
		state.draft = append([]rune{}, state.runes...)
	}
	next := state.recall + step
	if next < 0 {
		next = 0
	}
	if next > len(state.history) {
		next = len(state.history)
	}
	state.recall = next
	if next == len(state.history) {
		state.runes = append([]rune{}, state.draft...)
	} else {
		state.runes = append([]rune{}, state.history[next]...)
	}
	state.cursor = len(state.runes)
	return state
}

// renderLine перерисовывает строку ввода целиком и ставит курсор на место.
func renderLine(prompt string, state line, width int) string {
	visible := truncate(prompt+state.text(), width)
	frame := "\r\x1b[K" + visible
	tail := utf8.RuneCountInString(visible) - utf8.RuneCountInString(prompt) - state.cursor
	if tail > 0 {
		frame += "\x1b[" + strconv.Itoa(tail) + "D"
	}
	return frame
}

// fields разбивает введённую строку на слова команды.
func fields(text string) []string {
	return strings.Fields(text)
}
