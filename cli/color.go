package main

import (
	"os"
	"strconv"
	"strings"
)

// Собственная палитра вывода: тёплый янтарный акцент, приглушённый серый для
// второстепенного текста, спокойный зелёный и красный для состояний. Значения
// подобраны так, чтобы читаться и на тёмном, и на светлом фоне.
var (
	toneAccent = tone{0xE0, 0x8D, 0x3C, 173}
	toneMuted  = tone{0x8A, 0x85, 0x78, 102}
	toneGood   = tone{0x5F, 0xA9, 0x7F, 72}
	toneBad    = tone{0xC8, 0x5A, 0x54, 167}
)

// tone — цвет палитры: точное значение и ближайший индекс 256-цветного
// режима для терминалов без поддержки 24 бит.
type tone struct {
	red, green, blue int
	indexed          int
}

// Режимы вывода цвета.
const (
	colorNone = iota
	colorIndexed
	colorTrue
)

// colors — палитра вывода. По умолчанию выключена, поэтому тесты и любой
// неинтерактивный вызов получают чистый текст.
var colors palette

// palette хранит выбранный режим раскраски.
type palette struct {
	mode int
}

// newPalette выбирает режим: цвет уместен только в терминале и только если
// пользователь его не запретил.
func newPalette() palette {
	if os.Getenv("NO_COLOR") != "" {
		return palette{}
	}
	if term := os.Getenv("TERM"); term == "" || term == "dumb" {
		return palette{}
	}
	info, err := os.Stdout.Stat()
	if err != nil || info.Mode()&os.ModeCharDevice == 0 {
		return palette{}
	}
	colorTerm := strings.ToLower(os.Getenv("COLORTERM"))
	if strings.Contains(colorTerm, "truecolor") || strings.Contains(colorTerm, "24bit") {
		return palette{mode: colorTrue}
	}
	return palette{mode: colorIndexed}
}

// enabled сообщает, красится ли вывод.
func (colors palette) enabled() bool {
	return colors.mode != colorNone
}

// paint оборачивает текст в цвет палитры.
func (colors palette) paint(shade tone, text string) string {
	if colors.mode == colorNone || text == "" {
		return text
	}
	var code string
	if colors.mode == colorTrue {
		code = "38;2;" + strconv.Itoa(shade.red) + ";" +
			strconv.Itoa(shade.green) + ";" + strconv.Itoa(shade.blue)
	} else {
		code = "38;5;" + strconv.Itoa(shade.indexed)
	}
	return "\x1b[" + code + "m" + text + "\x1b[0m"
}

func (colors palette) accent(text string) string { return colors.paint(toneAccent, text) }
func (colors palette) muted(text string) string  { return colors.paint(toneMuted, text) }
func (colors palette) good(text string) string   { return colors.paint(toneGood, text) }
func (colors palette) bad(text string) string    { return colors.paint(toneBad, text) }

// clean убирает управляющие символы из строк, пришедших извне: имя проекта,
// статус docker и имя репозитория попадают на экран, и escape-последовательность
// оттуда выполнилась бы терминалом, а перевод строки сломал бы разметку.
func clean(text string) string {
	if strings.IndexFunc(text, control) < 0 {
		return text
	}
	return strings.Map(func(symbol rune) rune {
		if control(symbol) {
			return -1
		}
		return symbol
	}, text)
}

// control опознаёт управляющий символ, включая запись вне диапазона печати.
func control(symbol rune) bool {
	return symbol < 0x20 || symbol == 0x7f || (symbol >= 0x80 && symbol <= 0x9f) || symbol == 0xfeff
}

// visibleWidth считает ширину строки в символах, пропуская управляющие
// последовательности: от них на экране ничего не видно.
func visibleWidth(text string) int {
	width, escape := 0, false
	for _, symbol := range text {
		switch {
		case escape:
			if (symbol >= 'a' && symbol <= 'z') || (symbol >= 'A' && symbol <= 'Z') {
				escape = false
			}
		case symbol == 27:
			escape = true
		default:
			width++
		}
	}
	return width
}

// truncate обрезает строку по видимым символам, сохраняя раскраску и закрывая
// её сбросом, чтобы цвет не растекался на остальной экран.
func truncate(text string, width int) string {
	if visibleWidth(text) <= width {
		return text
	}
	var builder strings.Builder
	visible, escape, painted := 0, false, false
	for _, symbol := range text {
		switch {
		case escape:
			builder.WriteRune(symbol)
			if (symbol >= 'a' && symbol <= 'z') || (symbol >= 'A' && symbol <= 'Z') {
				escape = false
			}
			continue
		case symbol == 27:
			escape, painted = true, true
			builder.WriteRune(symbol)
			continue
		}
		if visible >= width-1 {
			break
		}
		builder.WriteRune(symbol)
		visible++
	}
	if width > 0 {
		builder.WriteString("…")
	}
	if painted {
		builder.WriteString("\x1b[0m")
	}
	return builder.String()
}
