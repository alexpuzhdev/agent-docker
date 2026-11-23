package main

import (
	"errors"
	"flag"
	"io"
	"regexp"
	"strconv"
	"strings"
)

// decimal повторяет проверку LOG_TAIL в manage.sh, чтобы отказ приходил от
// CLI с понятным сообщением, а не из глубины скрипта.
var decimal = regexp.MustCompile(`^(0|[1-9][0-9]{0,5})$`)

// Виды действий CLI. Всё, что меняет состояние гостя, выполняет manage.sh:
// CLI не дублирует его проверки, а только собирает для него окружение.
const (
	kindManage  = "manage"
	kindList    = "list"
	kindStatus  = "status"
	kindShell   = "shell"
	kindMenu    = "menu"
	kindResume  = "resume"
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
	// Без аргументов открывается список гостей: это основной экран. Строка
	// команд со слешем осталась отдельным режимом agd cmd.
	if len(args) == 0 {
		return action{kind: kindMenu}, nil
	}
	name := args[0]
	rest := args[1:]
	switch name {
	case "help", "-h", "--help":
		return bare(kindHelp, name, rest)
	case "version", "--version":
		return bare(kindVersion, name, rest)
	case "menu":
		return bare(kindMenu, name, rest)
	case "cmd":
		return bare(kindShell, name, rest)
	case "go", "resume":
		vm, err := singleVM(name, rest)
		return action{kind: kindResume, vm: vm}, err
	case "ls", "list":
		return bare(kindList, name, rest)
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

// bare собирает команду без аргументов и флагов, отклоняя лишнее: иначе
// опечатка вроде agd ls vm-02 выглядела бы выполненной.
func bare(kind, command string, rest []string) (action, error) {
	if len(rest) > 0 {
		return action{}, errors.New("команда " + command + " не принимает аргументы: " + strings.Join(rest, " "))
	}
	return action{kind: kind}, nil
}

// newFlags создаёт набор флагов, который сам ничего не печатает: сообщения
// об ошибках CLI выдаёт на русском в одном месте.
func newFlags(name string) *flag.FlagSet {
	flags := flag.NewFlagSet(name, flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	flags.Usage = func() {}
	return flags
}

// parse разбирает аргументы, не требуя, чтобы флаги стояли до имени VM.
// Стандартный разбор останавливается на первом позиционном аргументе,
// поэтому он повторяется для остатка, пока флаги не закончатся.
func parse(flags *flag.FlagSet, args []string) ([]string, error) {
	var positional []string
	for len(args) > 0 {
		if err := flags.Parse(args); err != nil {
			return nil, flagError(err)
		}
		rest := flags.Args()
		if len(rest) == 0 {
			break
		}
		positional = append(positional, rest[0])
		args = rest[1:]
	}
	return positional, nil
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
	positional, err := parse(flags, rest)
	if err != nil {
		return "", err
	}
	return takeVM(command, positional)
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
	positional, err := parse(flags, rest)
	if err != nil {
		return action{}, err
	}
	vm, err := takeVM("new", positional)
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
	arguments, err := parse(flags, rest)
	if err != nil {
		return action{}, err
	}
	environment := map[string]string{}
	vm := ""
	if len(arguments) > 0 {
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
	positional, err := parse(flags, rest)
	if err != nil {
		return action{}, err
	}
	vm, err := takeVM(command, positional)
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
	positional, err := parse(flags, rest)
	if err != nil {
		return action{}, err
	}
	vm, err := takeVM("git", positional)
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
	positional, err := parse(flags, rest)
	if err != nil {
		return action{}, err
	}
	vm, err := takeVM("logs", positional)
	if err != nil {
		return action{}, err
	}
	if *tail != "all" && !decimal.MatchString(*tail) {
		return action{}, errors.New("--tail принимает число от 0 до 999999 или all")
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
	positional, err := parse(flags, rest)
	if err != nil {
		return action{}, err
	}
	vm, err := takeVM("rm", positional)
	if err != nil {
		return action{}, err
	}
	if *keep && *drop {
		return action{}, errors.New("выберите либо --keep-disk, либо --delete-disk")
	}
	environment := map[string]string{"VM": vm, "DISK_KEEP": "1"}
	confirm := "Будут удалены контейнеры и настройки " + vm + ". Диск гостя сохранится в томе Docker."
	if *drop {
		environment["DISK_KEEP"] = "0"
		confirm = "Будут удалены контейнеры, диск и настройки " + vm + ", включая проекты и авторизацию в гостевой системе."
	}
	// Подтверждение снимает только явный --yes: --keep-disk выбирает судьбу
	// диска, но не отменяет вопрос о необратимом удалении.
	if *yes {
		confirm = ""
	}
	return action{kind: kindManage, vm: vm, target: "vm-remove",
		environment: environment, confirm: confirm}, nil
}
