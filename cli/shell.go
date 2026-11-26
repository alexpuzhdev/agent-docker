package main

import (
	"fmt"
	"io"
	"strings"
	"time"
)

// shellPrompt — приглашение строки команд.
const shellPrompt = "> "

// runShell показывает список гостей и строку команд. Команды начинаются со
// слеша и совпадают с подкомандами agd, а одно имя гостя открывает его сессию.
func runShell(root string, console streams) int {
	if !interactive() {
		fmt.Fprint(console.output, helpText)
		return 0
	}
	screen, err := openTerminal()
	if err != nil {
		fmt.Fprintln(console.errors, err)
		return 1
	}
	defer screen.cooked()
	quiet := streams{input: console.input, output: console.output, errors: io.Discard}
	vms, _ := inventory(root, quiet)
	fmt.Fprint(console.output, "\x1b[2J\x1b[H")
	showPanel(vms, root, console)
	state := line{}
	for {
		_, width := terminalSize()
		fmt.Fprint(console.output, renderLine(colors.accent(shellPrompt), state, width))
		data, err := readBytes()
		if err != nil {
			fmt.Fprint(console.output, "\r\n")
			return 0
		}
		var event lineEvent
		state, event = state.apply(data)
		switch event {
		case eventQuit:
			fmt.Fprint(console.output, "\r\n")
			return 0
		case eventInterrupt:
			fmt.Fprint(console.output, "\r\n")
		case eventComplete:
			// Дополняется только конец строки: иначе Tab уводил бы курсор.
			if state.cursor != len(state.runes) {
				continue
			}
			completed, candidates := completions(state.text(), vms)
			state = state.replace(completed)
			if len(candidates) > 1 {
				fmt.Fprint(console.output, "\r\n"+colors.muted(strings.Join(candidates, "   "))+"\r\n")
			}
		case eventSubmit:
			text := state.text()
			fmt.Fprint(console.output, "\r\n")
			if strings.TrimSpace(text) == "" {
				state = state.reset()
				continue
			}
			state = state.remember(text)
			done, err := runShellCommand(screen, root, text, console)
			if err != nil {
				fmt.Fprintln(console.errors, err)
				return 1
			}
			if done {
				return 0
			}
			vms, _ = inventory(root, quiet)
			if changesState(text) {
				showPanel(vms, root, console)
			}
		}
	}
}

// changing — команды, после которых состояние гостей могло измениться и
// список стоит показать заново.
var changing = map[string]bool{"go": true, "claude": true, "console": true, "up": true,
	"stop": true, "new": true, "setup": true, "rm": true, "remove": true, "proxy": true,
	"git": true, "auth": true, "logout": true, "ls": true, "list": true}

// changesState решает, перерисовывать ли список после команды. Вывод читающих
// команд остаётся на экране: панель его больше не затирает.
func changesState(text string) bool {
	arguments := shellArgs(text)
	if len(arguments) == 0 {
		return false
	}
	if arguments[0] == "git" && len(arguments) > 1 && arguments[1] == "status" {
		return false
	}
	return changing[arguments[0]]
}

// showPanel печатает список гостей потоком, не очищая экран.
func showPanel(vms []VM, root string, console streams) {
	_, width := terminalSize()
	fmt.Fprint(console.output, renderPanel(vms, root, width, time.Now()))
}

// runShellCommand выполняет введённую команду в обычном режиме терминала,
// чтобы запущенная консоль гостя получила тот же tty.
func runShellCommand(screen *terminal, root, text string, console streams) (bool, error) {
	arguments := shellArgs(text)
	if len(arguments) == 0 {
		return false, nil
	}
	if arguments[0] == "quit" {
		return true, nil
	}
	if strings.HasPrefix(arguments[0], "-") {
		fmt.Fprint(console.output, colors.muted("команды начинаются со слеша, например /ls\r\n"))
		return false, nil
	}
	screen.cooked()
	if code := execute(append([]string{"--root", root}, arguments...), console); code != 0 {
		fmt.Fprintf(console.errors, "команда завершилась с кодом %d\n", code)
	}
	return false, screen.raw()
}
