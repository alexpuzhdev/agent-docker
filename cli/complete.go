package main

import "strings"

// slashCommands — команды строки ввода. Их набор совпадает с подкомандами
// agd, поэтому разбор и проверки у них общие.
var slashCommands = []string{"/go", "/claude", "/shell", "/auth", "/logout", "/ls", "/status",
	"/up", "/stop", "/new", "/setup", "/rm", "/git", "/logs", "/profile", "/proxy",
	"/menu", "/help", "/quit"}

// completions подбирает продолжение введённой строки. Первое слово дополняется
// именем команды, остальные — именами гостей.
func completions(text string, vms []VM) (string, []string) {
	head, prefix := splitCompletion(text)
	var candidates []string
	if head == "" && strings.HasPrefix(prefix, "/") {
		candidates = matching(slashCommands, prefix)
	} else {
		names := make([]string, 0, len(vms))
		for _, vm := range vms {
			names = append(names, vm.Name)
		}
		candidates = matching(names, prefix)
	}
	if len(candidates) == 0 {
		return text, nil
	}
	completed := head + commonPrefix(candidates)
	if len(candidates) == 1 {
		completed += " "
	}
	return completed, candidates
}

// splitCompletion отделяет уже завершённую часть строки от дополняемого слова.
func splitCompletion(text string) (string, string) {
	if index := strings.LastIndex(text, " "); index >= 0 {
		return text[:index+1], text[index+1:]
	}
	return "", text
}

// matching оставляет варианты с заданным началом.
func matching(candidates []string, prefix string) []string {
	found := make([]string, 0, len(candidates))
	for _, candidate := range candidates {
		if strings.HasPrefix(candidate, prefix) {
			found = append(found, candidate)
		}
	}
	return found
}

// commonPrefix возвращает общее начало вариантов, чтобы Tab дополнял до него.
func commonPrefix(candidates []string) string {
	common := []rune(candidates[0])
	for _, candidate := range candidates[1:] {
		runes := []rune(candidate)
		if len(runes) < len(common) {
			common = common[:len(runes)]
		}
		for index := range common {
			if runes[index] != common[index] {
				common = common[:index]
				break
			}
		}
	}
	return string(common)
}

// shellArgs превращает введённую строку в аргументы подкоманды. Слеш в начале
// необязателен, а одно имя гостя означает возврат в его сессию.
func shellArgs(text string) []string {
	words := fields(text)
	if len(words) == 0 {
		return nil
	}
	if strings.HasPrefix(words[0], "/") {
		words[0] = strings.TrimPrefix(words[0], "/")
	} else if validName(words[0]) {
		words = append([]string{"go"}, words...)
	}
	if words[0] == "shell" {
		words[0] = "console"
	}
	if words[0] == "quit" || words[0] == "exit" {
		return []string{"quit"}
	}
	return words
}
