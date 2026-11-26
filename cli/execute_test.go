package main

import (
	"bufio"
	"bytes"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

// fakeProject создаёт каталог проекта с подставным manage.sh, который
// записывает цель и переданные переменные вместо работы с Docker. Список
// переменных берётся из самого CLI, поэтому новая переменная без теста
// не останется незамеченной.
func fakeProject(t *testing.T, code int) (string, string) {
	t.Helper()
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "v2"), 0o700); err != nil {
		t.Fatal(err)
	}
	log := filepath.Join(root, "calls.log")
	// Опрос канала сессий читающий, в журнал вызовов не попадает и отвечает
	// тем, что подложит fakeChannel: иначе каждый список гостей затирал бы
	// журнал и подменял цель, которую проверяет тест.
	script := "#!/bin/bash\n" +
		"if test \"$1\" = vm-sessions-check; then\n" +
		"  cat " + filepath.Join(root, "channel.json") + " 2>/dev/null\n" +
		"  exit 0\n" +
		"fi\n" +
		// Диагностика оболочки тоже читающая: она отвечает тем, что подложит
		// fakeShell, и журнал вызовов не затирает.
		"if test \"$1\" = vm-shell-check; then\n" +
		"  cat " + filepath.Join(root, "shell.json") + " 2>/dev/null\n" +
		"  exit 0\n" +
		"fi\n" +
		// Проверка прямого пути тоже читающая: по умолчанию его нет, и
		// fakeDirect подкладывает другой ответ там, где он нужен.
		"if test \"$1\" = vm-session-direct-check; then\n" +
		"  cat " + filepath.Join(root, "direct.json") + " 2>/dev/null ||" +
		" printf '{\"api\":1,\"direct\":\"absent\"}\\n'\n" +
		"  exit 0\n" +
		"fi\n{\n" +
		"printf 'target=%s\\n' \"$1\"\n" +
		"for name in " + strings.Join(managed, " ") + "; do\n" +
		"  if test -n \"${!name}\"; then printf '%s=%s\\n' \"$name\" \"${!name}\"; fi\n" +
		"done\n} > " + log + "\nexit " + strconv.Itoa(code) + "\n"
	if err := os.WriteFile(filepath.Join(root, marker), []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	return root, log
}

// fakeChannel подкладывает отчёт канала сессий, который отдаст подставной
// manage.sh, и забывает прежний опрос: кэш живёт несколько секунд и внутри
// одного теста отдал бы старый отчёт.
func fakeChannel(t *testing.T, root, report string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(root, "channel.json"), []byte(report+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	channelCache.clear()
	t.Cleanup(channelCache.clear)
}

// fakeShell подкладывает ответ читающей цели vm-shell-check: ждёт ли
// serial-оболочка гостя ввод или в ней работает приложение.
func fakeShell(t *testing.T, root, state string) {
	t.Helper()
	report := `{"api":1,"shell":"` + state + `"}`
	if err := os.WriteFile(filepath.Join(root, "shell.json"), []byte(report+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	channelCache.clear()
	t.Cleanup(channelCache.clear)
}

// fakeDirect подкладывает ответ читающей цели vm-session-direct-check: есть
// ли у гостя независимый от serial путь в сессии.
func fakeDirect(t *testing.T, root, state string) {
	t.Helper()
	report := `{"api":1,"direct":"` + state + `"}`
	if err := os.WriteFile(filepath.Join(root, "direct.json"), []byte(report+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	channelCache.clear()
	t.Cleanup(channelCache.clear)
}

// quietStreams собирает вывод команды без обращения к терминалу.
func quietStreams(input string) (streams, *bytes.Buffer, *bytes.Buffer) {
	output := &bytes.Buffer{}
	errorsOutput := &bytes.Buffer{}
	console := streams{input: bufio.NewReader(strings.NewReader(input)), output: output, errors: errorsOutput}
	return console, output, errorsOutput
}

func TestExecutePassesEnvironmentToManage(t *testing.T) {
	root, log := fakeProject(t, 0)
	console, _, errorsOutput := quietStreams("")
	code := execute([]string{"--root", root, "git", "--repo", "owner/name", "--write", "work", "vm-02"}, console)
	if code != 0 {
		t.Fatalf("код %d, ошибки: %s", code, errorsOutput)
	}
	recorded, err := os.ReadFile(log)
	if err != nil {
		t.Fatal(err)
	}
	expected := "target=vm-git\nVM=vm-02\nREPO=owner/name\nGIT_WRITE=1\nGIT_WRITE_BRANCH=work\n"
	if string(recorded) != expected {
		t.Fatalf("manage.sh получил:\n%s\nожидалось:\n%s", recorded, expected)
	}
}

func TestExecuteReturnsManageExitCode(t *testing.T) {
	root, _ := fakeProject(t, 3)
	console, _, _ := quietStreams("")
	if code := execute([]string{"--root", root, "stop", "vm-02"}, console); code != 3 {
		t.Fatalf("код %d, ожидался 3", code)
	}
}

func TestExecuteRejectsUnknownCommand(t *testing.T) {
	root, log := fakeProject(t, 0)
	console, _, errorsOutput := quietStreams("")
	if code := execute([]string{"--root", root, "destroy", "vm-02"}, console); code != 2 {
		t.Fatalf("код %d, ожидался 2", code)
	}
	if !strings.Contains(errorsOutput.String(), "неизвестная команда") {
		t.Fatalf("сообщение об ошибке: %s", errorsOutput)
	}
	if _, err := os.Stat(log); err == nil {
		t.Fatal("manage.sh не должен запускаться при ошибке разбора")
	}
}

func TestExecuteRemoveAsksConfirmation(t *testing.T) {
	root, log := fakeProject(t, 0)
	console, output, _ := quietStreams("нет\n")
	if code := execute([]string{"--root", root, "rm", "--delete-disk", "vm-02"}, console); code != 1 {
		t.Fatalf("код %d, ожидался 1", code)
	}
	if _, err := os.Stat(log); err == nil {
		t.Fatal("без подтверждения manage.sh не запускается")
	}
	if !strings.Contains(output.String(), "Отменено.") {
		t.Fatalf("вывод: %s", output)
	}

	console, _, errorsOutput := quietStreams("vm-02\n")
	if code := execute([]string{"--root", root, "rm", "--delete-disk", "vm-02"}, console); code != 0 {
		t.Fatalf("код %d, ошибки: %s", code, errorsOutput)
	}
	recorded, err := os.ReadFile(log)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(recorded), "DISK_KEEP=0") {
		t.Fatalf("manage.sh получил: %s", recorded)
	}
}

func TestExecuteListWithoutDocker(t *testing.T) {
	root, _ := fakeProject(t, 0)
	state := t.TempDir()
	writeVM(t, state, "vm-01", "2 4096 12\n", githubConfig(false, ""))
	t.Setenv("V2_STATE_DIR", state)
	t.Setenv("PATH", filepath.Join(root, "пусто"))
	console, output, errorsOutput := quietStreams("")
	if code := execute([]string{"--root", root, "ls"}, console); code != 0 {
		t.Fatalf("код %d, ошибки: %s", code, errorsOutput)
	}
	text := output.String()
	for _, expected := range []string{"vm-01", "неизвестно", "github owner/name"} {
		if !strings.Contains(text, expected) {
			t.Fatalf("в списке нет %q: %s", expected, text)
		}
	}
	if strings.Contains(text, fakeToken) {
		t.Fatal("токен попал в список")
	}
	if !strings.Contains(errorsOutput.String(), "docker") {
		t.Fatalf("о недоступном docker нужно сообщить: %s", errorsOutput)
	}
}

func TestExecuteHelpAndVersion(t *testing.T) {
	console, output, _ := quietStreams("")
	if code := execute([]string{"help"}, console); code != 0 {
		t.Fatalf("код %d", code)
	}
	text := output.String()
	// Справка перечисляет команды в таблице без префикса atrice, поэтому
	// проверяются строка использования, общий флаг и запись команды ls
	// вместе с её описанием.
	for _, expected := range []string{"atrice menu", "--root"} {
		if !strings.Contains(text, expected) {
			t.Fatalf("в справке нет %q: %s", expected, text)
		}
	}
	described := false
	for _, line := range strings.Split(text, "\n") {
		fields := strings.Fields(line)
		if len(fields) > 1 && fields[0] == "ls" && strings.Contains(line, "список гостей") {
			described = true
			break
		}
	}
	if !described {
		t.Fatalf("справка не описывает команду ls: %s", text)
	}
	console, output, _ = quietStreams("")
	if code := execute([]string{"version"}, console); code != 0 {
		t.Fatalf("код %d", code)
	}
	if !strings.Contains(output.String(), version) {
		t.Fatalf("версия: %s", output)
	}
}

// Интерактивные режимы без терминала печатают справку и не трогают manage.sh:
// иначе atrice в конвейере или из скрипта пытался бы захватить ввод.
func TestExecuteInteractiveModesWithoutTerminal(t *testing.T) {
	if interactive() {
		t.Skip("проверка рассчитана на ввод без терминала")
	}
	cases := []struct {
		name string
		args []string
	}{
		{"список по умолчанию", nil},
		{"список по имени", []string{"menu"}},
		{"строка команд", []string{"cmd"}},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			root, log := fakeProject(t, 0)
			console, output, errorsOutput := quietStreams("")
			if code := execute(append([]string{"--root", root}, test.args...), console); code != 0 {
				t.Fatalf("код %d, ошибки: %s", code, errorsOutput)
			}
			if !strings.Contains(output.String(), "atrice cmd") {
				t.Fatalf("без терминала показывается справка: %s", output)
			}
			if _, err := os.Stat(log); err == nil {
				t.Fatal("интерактивный режим не должен вызывать manage.sh")
			}
		})
	}
}

func TestExecuteIgnoresInheritedVariables(t *testing.T) {
	root, log := fakeProject(t, 0)
	// Экспортированные переменные не должны менять поведение команды:
	// atrice up без имени запускает всех настроенных гостей.
	t.Setenv("VM", "vm-99")
	t.Setenv("VCPU", "64")
	t.Setenv("PROJECT", "чужой")
	console, _, errorsOutput := quietStreams("")
	if code := execute([]string{"--root", root, "up"}, console); code != 0 {
		t.Fatalf("код %d, ошибки: %s", code, errorsOutput)
	}
	recorded, err := os.ReadFile(log)
	if err != nil {
		t.Fatal(err)
	}
	if string(recorded) != "target=vm-up\n" {
		t.Fatalf("manage.sh получил:\n%s", recorded)
	}
}

func TestExecuteAcceptsFlagsAfterName(t *testing.T) {
	root, log := fakeProject(t, 0)
	console, _, errorsOutput := quietStreams("")
	code := execute([]string{"logs", "vm-02", "--service", "proxy", "--tail", "100", "--follow",
		"--root", root}, console)
	if code != 0 {
		t.Fatalf("код %d, ошибки: %s", code, errorsOutput)
	}
	recorded, err := os.ReadFile(log)
	if err != nil {
		t.Fatal(err)
	}
	expected := "target=vm-logs\nVM=vm-02\nLOG_TAIL=100\nLOG_SERVICE=proxy\nLOG_FOLLOW=1\n"
	if string(recorded) != expected {
		t.Fatalf("manage.sh получил:\n%s\nожидалось:\n%s", recorded, expected)
	}
}
