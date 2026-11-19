package main

import (
	"bufio"
	"fmt"
	"os"
	"strings"
)

func main() {
	console := streams{input: bufio.NewReader(os.Stdin), output: os.Stdout, errors: os.Stderr}
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
	case kindShell:
		return runShell(root, console)
	case kindMenu:
		return runMenu(root, console)
	case kindResume:
		return resumeSession(root, act.vm, console)
	default:
		return performManage(root, act, console)
	}
}

// takeRoot снимает общий флаг --root, где бы он ни стоял: иначе команда с
// флагом после имени VM молча работала бы с автоматически найденным корнем.
func takeRoot(args []string) ([]string, string, error) {
	root := ""
	var rest []string
	for index := 0; index < len(args); index++ {
		if value, found := strings.CutPrefix(args[index], "--root="); found {
			if value == "" {
				return nil, "", errFlagRoot
			}
			root = value
			continue
		}
		if args[index] == "--root" {
			if index+1 >= len(args) || args[index+1] == "" {
				return nil, "", errFlagRoot
			}
			root = args[index+1]
			index++
			continue
		}
		rest = append(rest, args[index])
	}
	return rest, root, nil
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

// resumeSession возвращает пользователя в гостя: к работающему агенту через
// консоль, а в гостя без сессий — запуском агента.
func resumeSession(root, name string, console streams) int {
	vms, err := inventory(root, console)
	if err != nil {
		fmt.Fprintln(console.errors, err)
		return 1
	}
	for _, vm := range vms {
		if vm.Name != name {
			continue
		}
		if !vm.Configured() {
			fmt.Fprintf(console.errors, "%s ещё не настроена: agd new %s\n", name, name)
			return 1
		}
		target := lastSession(vm)
		return performManage(root, action{kind: kindManage, vm: name, target: target,
			environment: map[string]string{"VM": name}}, console)
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
	noteSession(root, act.vm, act.target, act.environment["PROJECT"], console)
	return exitCode(runManage(root, act.target, act.environment))
}
