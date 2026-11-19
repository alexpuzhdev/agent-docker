package main

import (
	"errors"
	"flag"
	"io"
	"strconv"
	"strings"
)

// Виды действий CLI. Всё, что меняет состояние гостя, выполняет manage.sh:
// CLI не дублирует его проверки, а только собирает для него окружение.
const (
	kindManage  = "manage"
	kindList    = "list"
	kindStatus  = "status"
	kindMenu    = "menu"
	kindHelp    = "help"
	kindVersion = "version"
)

// action — разобранная команда. Чистый результат разбора, пригодный для тестов.
type action struct {
	kind        string
	vm          string
	target      string
	environment map[string]string
	confirm     string
}

// parseArgs превращает аргументы командной строки в действие.
func parseArgs(args []string) (action, error) {
	if len(args) == 0 {
		return action{kind: kindMenu}, nil
	}
	name := args[0]
	rest := args[1:]
	switch name {
	case "help", "-h", "--help":
		return action{kind: kindHelp}, nil
	case "version", "--version":
		return action{kind: kindVersion}, nil
	case "menu":
		return action{kind: kindMenu}, nil
	case "ls", "list":
		return action{kind: kindList}, nil
	case "status":
		vm, err := singleVM(name, rest)
		return action{kind: kindStatus, vm: vm}, err
	case "new", "setup":
		return parseSetup(rest)
	case "up", "start":
		return parseUp(rest)
	case "stop":
		return simple(rest, "vm-stop", name)
	case "proxy":
		return simple(rest, "vm-proxy", name)
	case "profile":
		return simple(rest, "vm-profile", name)
	case "console":
		return parseConsole(rest, "vm-console", name)
	case "claude":
		return parseConsole(rest, "claude", name)
	case "auth":
		return simple(rest, "claude-auth", name)
	case "logout":
		return simple(rest, "claude-logout", name)
	case "git":
		return parseGit(rest)
	case "logs":
		return parseLogs(rest)
	case "rm", "remove":
		return parseRemove(rest)
	default:
		return action{}, errors.New("неизвестная команда " + name + ". Список команд: agd help")
	}
}

// newFlags создаёт набор флагов, который сам ничего не печатает: сообщения
// об ошибках CLI выдаёт на русском в одном месте.
func newFlags(name string) *flag.FlagSet {
	flags := flag.NewFlagSet(name, flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	flags.Usage = func() {}
	return flags
}

// takeVM проверяет, что остался ровно один позиционный аргумент — имя VM.
func takeVM(command string, rest []string) (string, error) {
	if len(rest) == 0 {
		return "", errors.New("укажите VM: agd " + command + " vm-01")
	}
	if len(rest) > 1 {
		return "", errors.New("лишние аргументы после имени VM: " + strings.Join(rest[1:], " "))
	}
	if !validName(rest[0]) {
		return "", errors.New("имя VM задаётся как vm-01, номера от 01 до 254")
	}
	return rest[0], nil
}

// singleVM разбирает команду без флагов, у которой есть только имя VM.
func singleVM(command string, rest []string) (string, error) {
	flags := newFlags(command)
	if err := flags.Parse(rest); err != nil {
		return "", flagError(err)
	}
	return takeVM(command, flags.Args())
}

// simple собирает цель manage.sh, которой нужно только имя VM.
func simple(rest []string, target, command string) (action, error) {
	vm, err := singleVM(command, rest)
	if err != nil {
		return action{}, err
	}
	return action{kind: kindManage, vm: vm, target: target,
		environment: map[string]string{"VM": vm}}, nil
}

func flagError(err error) error {
	if errors.Is(err, flag.ErrHelp) {
		return errors.New("список команд и флагов: agd help")
	}
	return errors.New(strings.TrimPrefix(err.Error(), "flag provided but not defined: ") +
		" — проверьте флаги, справка: agd help")
}

// parseSetup разбирает создание или повторную подготовку гостя.
func parseSetup(rest []string) (action, error) {
	flags := newFlags("new")
	cpu := flags.Int("cpu", 0, "")
	ram := flags.Int("ram", 0, "")
	disk := flags.Int("disk", 0, "")
	repository := flags.String("repo", "", "")
	provider := flags.String("provider", "", "")
	origin := flags.String("url", "", "")
	branch := flags.String("write", "", "")
	sni := flags.Bool("sni-log", false, "")
	if err := flags.Parse(rest); err != nil {
		return action{}, flagError(err)
	}
	vm, err := takeVM("new", flags.Args())
	if err != nil {
		return action{}, err
	}
	environment := map[string]string{"VM": vm}
	for key, value := range map[string]int{"VCPU": *cpu, "RAM_MB": *ram, "DISK_GB": *disk} {
		if value < 0 {
			return action{}, errors.New("ресурсы задаются положительными числами")
		}
		if value > 0 {
			environment[key] = strconv.Itoa(value)
		}
	}
	if err := applyGit(environment, *repository, *provider, *origin, *branch, false); err != nil {
		return action{}, err
	}
	applySNI(environment, *sni)
	return action{kind: kindManage, vm: vm, target: "vm-setup", environment: environment}, nil
}

// parseUp разбирает запуск одного гостя или всех настроенных.
func parseUp(rest []string) (action, error) {
	flags := newFlags("up")
	sni := flags.Bool("sni-log", false, "")
	if err := flags.Parse(rest); err != nil {
		return action{}, flagError(err)
	}
	environment := map[string]string{}
	vm := ""
	if arguments := flags.Args(); len(arguments) > 0 {
		parsed, err := takeVM("up", arguments)
		if err != nil {
			return action{}, err
		}
		vm = parsed
		environment["VM"] = vm
	}
	applySNI(environment, *sni)
	return action{kind: kindManage, vm: vm, target: "vm-up", environment: environment}, nil
}

// parseConsole разбирает вход в консоль гостя и запуск агента.
func parseConsole(rest []string, target, command string) (action, error) {
	flags := newFlags(command)
	project := flags.String("project", "", "")
	resume := flags.String("resume", "", "")
	if err := flags.Parse(rest); err != nil {
		return action{}, flagError(err)
	}
	vm, err := takeVM(command, flags.Args())
	if err != nil {
		return action{}, err
	}
	if *resume != "" && target != "claude" {
		return action{}, errors.New("--resume относится только к agd claude")
	}
	environment := map[string]string{"VM": vm}
	if *project != "" {
		environment["PROJECT"] = *project
	}
	if *resume != "" {
		environment["RESUME"] = *resume
	}
	return action{kind: kindManage, vm: vm, target: target, environment: environment}, nil
}

// parseGit разбирает назначение репозитория и просмотр настройки.
func parseGit(rest []string) (action, error) {
	if len(rest) > 0 && rest[0] == "status" {
		vm, err := singleVM("git status", rest[1:])
		if err != nil {
			return action{}, err
		}
		return action{kind: kindManage, vm: vm, target: "vm-git-status",
			environment: map[string]string{"VM": vm}}, nil
	}
	flags := newFlags("git")
	repository := flags.String("repo", "", "")
	provider := flags.String("provider", "", "")
	origin := flags.String("url", "", "")
	branch := flags.String("write", "", "")
	if err := flags.Parse(rest); err != nil {
		return action{}, flagError(err)
	}
	vm, err := takeVM("git", flags.Args())
	if err != nil {
		return action{}, err
	}
	environment := map[string]string{"VM": vm}
	if err := applyGit(environment, *repository, *provider, *origin, *branch, true); err != nil {
		return action{}, err
	}
	return action{kind: kindManage, vm: vm, target: "vm-git", environment: environment}, nil
}

// applyGit переносит параметры репозитория в окружение manage.sh. Разбор
// адреса и имени проекта остаётся за manage.sh, здесь проверяется только
// согласованность набора флагов.
func applyGit(environment map[string]string, repository, provider, origin, branch string, required bool) error {
	if repository == "" {
		if provider != "" || origin != "" || branch != "" {
			return errors.New("укажите репозиторий через --repo")
		}
		if required {
			return errors.New("укажите репозиторий: agd git vm-01 --repo owner/name")
		}
		return nil
	}
	environment["REPO"] = repository
	if provider != "" {
		if provider != "github" && provider != "gitlab" {
			return errors.New("--provider принимает github или gitlab")
		}
		environment["GIT_PROVIDER"] = provider
	}
	if origin != "" {
		if provider != "gitlab" {
			return errors.New("--url задаётся только вместе с --provider gitlab")
		}
		environment["GIT_URL"] = origin
	}
	if provider == "gitlab" && origin == "" {
		return errors.New("для GitLab укажите адрес сервера через --url")
	}
	if branch != "" {
		environment["GIT_WRITE"] = "1"
		environment["GIT_WRITE_BRANCH"] = branch
	} else {
		environment["GIT_WRITE"] = "0"
	}
	return nil
}

// applySNI включает журнал SNI на шлюзе выбранного гостя.
func applySNI(environment map[string]string, enabled bool) {
	if enabled {
		environment["GATEWAY_SNI_LOG"] = "1"
	}
}

// parseLogs разбирает просмотр журналов контейнеров гостя.
func parseLogs(rest []string) (action, error) {
	flags := newFlags("logs")
	tail := flags.String("tail", "50", "")
	service := flags.String("service", "", "")
	follow := flags.Bool("follow", false, "")
	if err := flags.Parse(rest); err != nil {
		return action{}, flagError(err)
	}
	vm, err := takeVM("logs", flags.Args())
	if err != nil {
		return action{}, err
	}
	if *tail != "all" {
		lines, err := strconv.Atoi(*tail)
		if err != nil || lines < 0 || lines > 100000 {
			return action{}, errors.New("--tail принимает число от 0 до 100000 или all")
		}
	}
	if *service != "" && *service != "vm" && *service != "proxy" {
		return action{}, errors.New("--service принимает vm или proxy")
	}
	environment := map[string]string{"VM": vm, "LOG_TAIL": *tail, "LOG_SERVICE": *service}
	if *follow {
		environment["LOG_FOLLOW"] = "1"
	}
	return action{kind: kindManage, vm: vm, target: "vm-logs", environment: environment}, nil
}

// parseRemove разбирает удаление гостя. Диск сохраняется, пока его удаление
// не запрошено явно.
func parseRemove(rest []string) (action, error) {
	flags := newFlags("rm")
	keep := flags.Bool("keep-disk", false, "")
	drop := flags.Bool("delete-disk", false, "")
	yes := flags.Bool("yes", false, "")
	if err := flags.Parse(rest); err != nil {
		return action{}, flagError(err)
	}
	vm, err := takeVM("rm", flags.Args())
	if err != nil {
		return action{}, err
	}
	if *keep && *drop {
		return action{}, errors.New("выберите либо --keep-disk, либо --delete-disk")
	}
	environment := map[string]string{"VM": vm, "DISK_KEEP": "1"}
	confirm := ""
	if *drop {
		environment["DISK_KEEP"] = "0"
		confirm = "Будут удалены контейнеры, диск и настройки " + vm + ", включая проекты и авторизацию в гостевой системе."
	} else if !*keep {
		confirm = "Будут удалены контейнеры и настройки " + vm + ". Диск гостя сохранится в томе Docker."
	}
	if *yes {
		confirm = ""
	}
	return action{kind: kindManage, vm: vm, target: "vm-remove",
		environment: environment, confirm: confirm}, nil
}
