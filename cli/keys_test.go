package main

import "testing"

// Русская раскладка должна вызывать те же действия, что и латинская: буква в
// UTF-8 занимает два байта, и разбор по первому байту сводил всю раскладку к
// 0xD0 и 0xD1 — ни одно действие с неё не вызывалось.
func TestDecodeKeyFoldsRussianLayout(t *testing.T) {
	cases := map[string]rune{
		"с": 'c', "ф": 'a', "г": 'u', "ы": 's', "п": 'g', "д": 'l',
		"з": 'p', "в": 'd', "т": 'n', "к": 'r', "й": 'q', "о": 'j', "л": 'k',
		// Заглавные буквы приходят с Shift и Caps Lock — то же действие.
		"С": 'c', "Й": 'q', "C": 'c', "Q": 'q',
	}
	for typed, expected := range cases {
		if got := decodeKey([]byte(typed)); got != expected {
			t.Fatalf("клавиша %q дала %q, ожидалось %q", typed, got, expected)
		}
	}
}

// Каждая буква действия должна достигаться с русской раскладки: иначе часть
// списка оставалась бы доступной только при переключении.
func TestRussianLayoutReachesEveryAction(t *testing.T) {
	reachable := make(map[rune]string)
	for russian := range layout {
		reachable[foldKey(russian)] = string(russian)
	}
	for key, target := range keyActions {
		russian, known := reachable[key]
		if !known {
			t.Fatalf("клавиша %q цели %s недостижима с русской раскладки", key, target)
		}
		if keyActions[decodeKey([]byte(russian))] != target {
			t.Fatalf("русская %q должна вызывать %s", russian, target)
		}
	}
	for _, key := range []rune{'q', 'j', 'k', 'r', 'n'} {
		russian, known := reachable[key]
		if !known {
			t.Fatalf("навигация %q недостижима с русской раскладки", key)
		}
		if navigation(decodeKey([]byte(russian))) != navigation(key) {
			t.Fatalf("русская %q должна работать как %q", russian, key)
		}
	}
}

// Раскладка свёрнута по расположению клавиш, поэтому разные русские буквы не
// должны попадать на одну латинскую: иначе одно действие отняло бы клавишу у
// другого.
func TestLayoutIsOneToOne(t *testing.T) {
	seen := make(map[rune]rune, len(layout))
	for russian, latin := range layout {
		if previous, busy := seen[latin]; busy {
			t.Fatalf("латинская %q занята и буквой %q, и буквой %q", latin, previous, russian)
		}
		seen[latin] = russian
	}
}

func TestDecodeKeySpecialKeys(t *testing.T) {
	cases := []struct {
		name     string
		data     []byte
		expected rune
	}{
		{"Enter", []byte{13}, keyEnter},
		{"стрелка вверх", []byte{27, '[', 'A'}, keyUp},
		{"стрелка вниз", []byte{27, '[', 'B'}, keyDown},
		{"стрелка вправо", []byte{27, '[', 'C'}, keyOther},
		{"Esc", []byte{27}, keyEscape},
		{"Ctrl+]", []byte{29}, keyDetach},
		{"Ctrl+C", []byte{3}, keyInterrupt},
		{"Ctrl+D", []byte{4}, keyEndOfStream},
		{"пустое чтение", nil, keyOther},
		{"не UTF-8", []byte{0xD1}, keyOther},
	}
	for _, test := range cases {
		if got := decodeKey(test.data); got != test.expected {
			t.Fatalf("%s: получено %q, ожидалось %q", test.name, got, test.expected)
		}
	}
	// Ctrl+] и Esc не должны оказаться действиями над гостем: ими возвращают.
	for _, key := range []rune{keyDetach, keyEscape, keyOther} {
		if target, known := keyActions[key]; known {
			t.Fatalf("клавиша возврата %q вызывает %s", key, target)
		}
	}
}
