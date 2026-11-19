package main

import (
	"strings"
	"unicode/utf8"
)

// hints — подсказки по клавишам, по две строки, чтобы помещаться в узкий терминал.
var hints = []string{
	"c консоль   a Claude   u запустить   s остановить   n создать",
	"g git       l логи     p профиль     d удалить      r обновить   q выход",
}

// menuAction — действие, выбранное клавишей в меню.
type menuAction string

const (
	actionNone    menuAction = ""
	actionQuit    menuAction = "quit"
	actionUp      menuAction = "up"
	actionDown    menuAction = "down"
	actionRefresh menuAction = "refresh"
	actionCreate  menuAction = "create"
)

// keyActions — клавиши, которым соответствует цель manage.sh.
var keyActions = map[byte]string{
	'c': "vm-console",
	'a': "claude",
	'u': "vm-up",
	's': "vm-stop",
	'g': "vm-git-status",
	'l': "vm-logs",
	'p': "vm-profile",
	'd': "vm-remove",
}

// navigation переводит нажатую клавишу в действие навигации.
func navigation(key byte) menuAction {
	switch key {
	case 'q', 3, 4:
		return actionQuit
	case 'k', 'A':
		return actionUp
	case 'j', 'B':
		return actionDown
	case 'r':
		return actionRefresh
	case 'n':
		return actionCreate
	}
	return actionNone
}

// moveCursor сдвигает курсор, останавливаясь на границах списка.
func moveCursor(current, delta, count int) int {
	if count == 0 {
		return 0
	}
	next := current + delta
	if next < 0 {
		return 0
	}
	if next >= count {
		return count - 1
	}
	return next
}

// windowStart выбирает начало видимого окна так, чтобы курсор оставался внутри.
func windowStart(cursor, visible, count int) int {
	if visible <= 0 || count <= visible {
		return 0
	}
	start := cursor - visible/2
	if start < 0 {
		start = 0
	}
	if start > count-visible {
		start = count - visible
	}
	return start
}

// renderMenu строит кадр меню. Строки разделяются \r\n: терминал находится в
// неформатированном режиме и сам каретку не возвращает.
func renderMenu(vms []VM, cursor, width, height int, root, notice string) string {
	if width < 20 {
		width = 20
	}
	rows := make([][]string, 0, len(vms))
	for _, vm := range vms {
		rows = append(rows, vmRow(vm))
	}
	table := strings.Split(strings.TrimRight(renderTable(listHeaders, rows), "\n"), "\n")
	header := table[0]
	body := table[1:]
	if len(vms) == 0 {
		body = []string{"гостей нет, создайте первого клавишей n"}
	}
	// Кадр занимает ровно height строк: заголовок, корень, пустая строка,
	// шапка таблицы, список, пустая строка, подсказки и строка сообщения.
	visible := height - (6 + len(hints))
	if visible < 1 {
		visible = 1
	}
	start := windowStart(cursor, visible, len(body))
	lines := []string{
		truncate("agd "+version+" — agent-docker", width),
		truncate(root, width),
		"",
		truncate("  "+header, width),
	}
	for index := start; index < len(body) && index < start+visible; index++ {
		prefix := "  "
		if index == cursor && len(vms) > 0 {
			prefix = "> "
		}
		lines = append(lines, truncate(prefix+body[index], width))
	}
	lines = append(lines, "")
	for _, hint := range hints {
		lines = append(lines, truncate(hint, width))
	}
	lines = append(lines, truncate(notice, width))
	// В очень низком окне кадр обрезается: прокрутка терминала сдвинула бы
	// верх списка за край экрана.
	if len(lines) > height {
		lines = lines[:height]
	}
	return strings.Join(lines, "\r\n")
}

// truncate обрезает строку по числу символов, а не байтов.
func truncate(line string, width int) string {
	if utf8.RuneCountInString(line) <= width {
		return line
	}
	runes := []rune(line)
	if width <= 1 {
		return string(runes[:width])
	}
	return string(runes[:width-1]) + "…"
}
