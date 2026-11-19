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
	showPanel(vms, root, console)
	state := line{}
	for {
		_, width := terminalSize()
		fmt.Fprint(console.output, renderLine(shellPrompt, state, width))
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
			completed, candidates := completions(state.text(), vms)
			state = state.replace(completed)
			if len(candidates) > 1 {
				fmt.Fprint(console.output, "\r\n"+strings.Join(candidates, "   ")+"\r\n")
			}
		case eventSubmit:
			text := state.text()
			state = state.remember(text)
			fmt.Fprint(console.output, "\r\n")
			if strings.TrimSpace(text) == "" {
				continue
			}
			done, err := runShellCommand(screen, root, text, console)
			if err != nil {
				fmt.Fprintln(console.errors, err)
				return 1
			}
			if done {
				return 0
			}
			vms, _ = inventory(root, quiet)
			showPanel(vms, root, console)
		}
	}
}

// showPanel перерисовывает экран со списком гостей.
func showPanel(vms []VM, root string, console streams) {
	_, width := terminalSize()
	fmt.Fprint(console.output, "\x1b[2J\x1b[H"+renderPanel(vms, root, width, time.Now()))
}

// runShellCommand выполняет введённую команду в обычном режиме терминала,
// чтобы запущенная консоль гостя получила тот же tty.
func runShellCommand(screen *terminal, root, text string, console streams) (bool, error) {
	arguments := shellArgs(text)
	if len(arguments) == 0 {
		return false, nil
	}
	if arguments[0] == "quit" {
		fmt.Fprint(console.output, "\r\n")
		return true, nil
	}
	screen.cooked()
	fmt.Fprint(console.output, "\x1b[2J\x1b[H")
	execute(append([]string{"--root", root}, arguments...), console)
	return false, screen.raw()
}
