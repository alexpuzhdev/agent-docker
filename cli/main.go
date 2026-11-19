package main

import (
	"fmt"
	"os"
	"strings"
)

func main() {
	console := streams{input: os.Stdin, output: os.Stdout, errors: os.Stderr}
	os.Exit(execute(os.Args[1:], console))
}

// execute разбирает аргументы, выполняет действие и возвращает код выхода.
// Код 2 означает ошибку в самой команде, остальные коды приходят от manage.sh.
func execute(args []string, console streams) int {
	args, explicit, err := takeRoot(args)
	if err != nil {
		fmt.Fprintln(console.errors, err)
		return 2
	}
	act, err := parseArgs(args)
	if err != nil {
		fmt.Fprintln(console.errors, err)
		return 2
	}
	switch act.kind {
	case kindHelp:
		fmt.Fprint(console.output, helpText)
		return 0
	case kindVersion:
		fmt.Fprintln(console.output, "agd "+version)
		return 0
	}
	root, err := findRoot(explicit)
	if err != nil {
		fmt.Fprintln(console.errors, err)
		return 2
	}
	switch act.kind {
	case kindList:
		return showList(root, console)
	case kindStatus:
		return showStatus(root, act.vm, console)
	case kindMenu:
		return runMenu(root, console)
	default:
		return performManage(root, act, console)
	}
}

// takeRoot снимает общий флаг --root, который допускается перед командой.
func takeRoot(args []string) ([]string, string, error) {
	if len(args) == 0 {
		return args, "", nil
	}
	if value, found := strings.CutPrefix(args[0], "--root="); found {
		if value == "" {
			return nil, "", errFlagRoot
		}
		return args[1:], value, nil
	}
	if args[0] == "--root" {
		if len(args) < 2 || args[1] == "" {
			return nil, "", errFlagRoot
		}
		return args[2:], args[1], nil
	}
	return args, "", nil
}

// inventory читает состояние гостей и дополняет его состоянием контейнеров.
// Недоступный docker не скрывает список: сведения о контейнерах помечаются
// как неизвестные, а причина уходит в поток ошибок.
func inventory(root string, console streams) ([]VM, error) {
	vms, err := loadVMs(stateRoot(root))
	if err != nil {
		return nil, err
	}
	found, err := statuses(projectPrefix())
	if err != nil {
		fmt.Fprintln(console.errors, err)
		return vms, nil
	}
	applyStatuses(vms, found)
	return vms, nil
}

func showList(root string, console streams) int {
	vms, err := inventory(root, console)
	if err != nil {
		fmt.Fprintln(console.errors, err)
		return 1
	}
	fmt.Fprint(console.output, renderList(vms))
	return 0
}

func showStatus(root, name string, console streams) int {
	vms, err := inventory(root, console)
	if err != nil {
		fmt.Fprintln(console.errors, err)
		return 1
	}
	for _, vm := range vms {
		if vm.Name == name {
			fmt.Fprint(console.output, renderStatus(vm))
			return 0
		}
	}
	fmt.Fprintf(console.errors, "%s не найдена. Создайте её: agd new %s\n", name, name)
	return 1
}

// performManage выполняет цель manage.sh, спросив подтверждение там, где
// действие необратимо.
func performManage(root string, act action, console streams) int {
	if act.confirm != "" && !confirmed(act.confirm, act.vm, console) {
		fmt.Fprintln(console.output, "Отменено.")
		return 1
	}
	return exitCode(runManage(root, act.target, act.environment))
}
