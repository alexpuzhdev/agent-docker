package main

import (
	"reflect"
	"strings"
	"testing"
)

func TestCompletionsForCommands(t *testing.T) {
	vms := []VM{{Name: "vm-01"}, {Name: "vm-02"}}
	completed, candidates := completions("/prof", vms)
	if completed != "/profile " || !reflect.DeepEqual(candidates, []string{"/profile"}) {
		t.Fatalf("получено %q %v", completed, candidates)
	}
	// Несколько совпадений дополняются до общего начала, порядок — как в списке команд.
	completed, candidates = completions("/pro", vms)
	if completed != "/pro" || !reflect.DeepEqual(candidates, []string{"/profile", "/proxy"}) {
		t.Fatalf("получено %q %v", completed, candidates)
	}
	completed, candidates = completions("/l", vms)
	if completed != "/l" || !reflect.DeepEqual(candidates, []string{"/logout", "/ls", "/logs"}) {
		t.Fatalf("получено %q %v", completed, candidates)
	}
	completed, _ = completions("/lo", vms)
	if completed != "/log" {
		t.Fatalf("дополнение до общего начала дало %q", completed)
	}
}

func TestCompletionsForGuests(t *testing.T) {
	vms := []VM{{Name: "vm-01"}, {Name: "vm-02"}, {Name: "vm-10"}}
	completed, candidates := completions("/go vm-1", vms)
	if completed != "/go vm-10 " || len(candidates) != 1 {
		t.Fatalf("получено %q %v", completed, candidates)
	}
	completed, candidates = completions("/go ", vms)
	if completed != "/go vm-" || len(candidates) != 3 {
		t.Fatalf("получено %q %v", completed, candidates)
	}
	completed, candidates = completions("/go vm-9", vms)
	if completed != "/go vm-9" || candidates != nil {
		t.Fatalf("без совпадений строка не меняется: %q %v", completed, candidates)
	}
}

func TestShellArgs(t *testing.T) {
	cases := map[string][]string{
		"/ls":                  {"ls"},
		"ls":                   {"ls"},
		"/logs vm-02 --tail 5": {"logs", "vm-02", "--tail", "5"},
		"vm-02":                {"go", "vm-02"},
		"/shell vm-02":         {"console", "vm-02"},
		"/quit":                {"quit"},
		"exit":                 {"quit"},
		"   ":                  nil,
	}
	for text, expected := range cases {
		if got := shellArgs(text); !reflect.DeepEqual(got, expected) {
			t.Fatalf("%q дало %v, ожидалось %v", text, got, expected)
		}
	}
}

func TestSlashCommandsMatchSubcommands(t *testing.T) {
	// Каждая команда строки должна разбираться обычным разбором аргументов.
	for _, command := range slashCommands {
		name := strings.TrimPrefix(command, "/")
		// Команды без имени гостя и git, который требует хотя бы --repo,
		// проверяются отдельно в тестах разбора аргументов.
		if name == "quit" || name == "menu" || name == "ls" || name == "help" || name == "git" {
			continue
		}
		arguments := shellArgs(command + " vm-01")
		if _, err := parseArgs(arguments); err != nil {
			t.Fatalf("команда %q не разбирается: %v", command, err)
		}
	}
}

func TestChangesStateOnlyForCommandsThatChangeIt(t *testing.T) {
	refreshes := []string{"/up vm-01", "/stop vm-01", "/go vm-01", "/claude vm-01",
		"/rm vm-01", "/git vm-01 --repo owner/name", "/ls", "vm-01"}
	for _, text := range refreshes {
		if !changesState(text) {
			t.Fatalf("после %q список нужно обновить", text)
		}
	}
	keeps := []string{"/logs vm-01", "/status vm-01", "/profile vm-01", "/help",
		"/git status vm-01", "", "/quit"}
	for _, text := range keeps {
		if changesState(text) {
			t.Fatalf("после %q вывод не должен затираться списком", text)
		}
	}
}
