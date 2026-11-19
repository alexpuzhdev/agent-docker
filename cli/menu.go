package main

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

// terminal переключает режим ввода через stty: меню читает отдельные
// клавиши, а запущенные из него команды получают обычный терминал.
type terminal struct {
	saved string
}

// openTerminal запоминает режим терминала и включает неформатированный ввод.
func openTerminal() (*terminal, error) {
	saved, err := stty("-g")
	if err != nil {
		return nil, fmt.Errorf("не удалось прочитать режим терминала: %w", err)
	}
	console := &terminal{saved: saved}
	if err := console.raw(); err != nil {
		return nil, err
	}
	return console, nil
}

func (console *terminal) raw() error {
	if _, err := stty("raw", "-echo"); err != nil {
		return fmt.Errorf("не удалось переключить терминал: %w", err)
	}
	return nil
}

func (console *terminal) cooked() {
	if console.saved != "" {
		stty(strings.Fields(console.saved)...)
	}
}

// stty выполняет команду над текущим терминалом.
func stty(args ...string) (string, error) {
	command := exec.Command("stty", args...)
	command.Stdin = os.Stdin
	output, err := command.Output()
	return strings.TrimSpace(string(output)), err
}

// terminalSize возвращает размер окна, подставляя обычные 24x80 при отказе.
func terminalSize() (int, int) {
	output, err := stty("size")
	if err == nil {
		fields := strings.Fields(output)
		if len(fields) == 2 {
			height, heightErr := strconv.Atoi(fields[0])
			width, widthErr := strconv.Atoi(fields[1])
			if heightErr == nil && widthErr == nil && height > 4 && width > 20 {
				return height, width
			}
		}
	}
	return 24, 80
}

// interactive сообщает, подключён ли к вводу терминал. Признака символьного
// устройства мало: /dev/null тоже символьное, поэтому режим запрашивается у
// самого терминала, иначе agd </dev/null падал бы вместо показа справки.
func interactive() bool {
	info, err := os.Stdin.Stat()
	if err != nil || info.Mode()&os.ModeCharDevice == 0 {
		return false
	}
	_, err = stty("-g")
	return err == nil
}

// readBytes читает очередную порцию ввода терминала.
func readBytes() ([]byte, error) {
	buffer := make([]byte, 32)
	count, err := os.Stdin.Read(buffer)
	if err != nil {
		return nil, err
	}
	if count == 0 {
		return nil, io.EOF
	}
	return buffer[:count], nil
}

// readKey читает одну клавишу, разбирая последовательности стрелок.
func readKey() (byte, error) {
	data, err := readBytes()
	if err != nil {
		return 0, err
	}
	if len(data) >= 3 && data[0] == 27 && data[1] == '[' {
		return data[2], nil
	}
	return data[0], nil
}

// runMenu показывает список окружений и выполняет над ними действия.
// Без терминала меню открыть нельзя, поэтому печатается справка.
func runMenu(root string, console streams) int {
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
	// Пока открыто меню, сообщения о недоступном docker не портят кадр:
	// их место занимает строка состояния под списком.
	quiet := streams{input: console.input, output: console.output, errors: io.Discard}
	cursor := 0
	notice := ""
	for {
		vms, err := inventory(root, quiet)
		if err != nil {
			notice = err.Error()
			vms = nil
		}
		cursor = moveCursor(cursor, 0, len(vms))
		height, width := terminalSize()
		fmt.Fprint(console.output, "\x1b[2J\x1b[H"+
			renderMenu(vms, cursor, width, height, root, notice, time.Now()))
		key, err := readKey()
		if err != nil {
			return 0
		}
		notice = ""
		switch navigation(key) {
		case actionQuit:
			fmt.Fprint(console.output, "\x1b[2J\x1b[H")
			return 0
		case actionUp:
			cursor = moveCursor(cursor, -1, len(vms))
			continue
		case actionDown:
			cursor = moveCursor(cursor, 1, len(vms))
			continue
		case actionRefresh:
			continue
		case actionCreate:
			if err := outside(screen, console, func() { createVM(root, console) }); err != nil {
				fmt.Fprintln(console.errors, err)
				return 1
			}
			continue
		}
		target, known := keyActions[key]
		if !known && key != 13 {
			continue
		}
		if len(vms) == 0 {
			notice = "сначала создайте гостя клавишей n"
			continue
		}
		selected := vms[cursor]
		project := ""
		if key == 13 {
			// Enter возвращает туда, где работали в прошлый раз.
			target = lastSession(selected)
		}
		if err := outside(screen, console, func() { menuTarget(root, target, selected, project, console) }); err != nil {
			fmt.Fprintln(console.errors, err)
			return 1
		}
	}
}

// outside выполняет действие в обычном режиме терминала и возвращает меню
// в неформатированный режим. Если вернуть режим не удалось, меню закрывается:
// продолжать в построчном вводе означало бы не реагировать на клавиши.
func outside(screen *terminal, console streams, body func()) error {
	screen.cooked()
	fmt.Fprint(console.output, "\x1b[2J\x1b[H")
	body()
	fmt.Fprint(console.output, "\nНажмите Enter, чтобы вернуться в меню. ")
	console.input.ReadString('\n')
	return screen.raw()
}

// menuTarget запускает выбранное действие над гостем.
func menuTarget(root, target string, vm VM, project string, console streams) {
	environment := map[string]string{"VM": vm.Name}
	if project != "" {
		environment["PROJECT"] = project
	}
	switch target {
	case "vm-logs":
		environment["LOG_TAIL"] = "50"
		environment["LOG_SERVICE"] = ""
	case "vm-remove":
		// Диск гостя из меню не удаляется: для этого есть agd rm --delete-disk.
		environment["DISK_KEEP"] = "1"
		prompt := "Будут удалены контейнеры и настройки " + vm.Name + ". Диск гостя сохранится в томе Docker."
		if !confirmed(prompt, vm.Name, console) {
			fmt.Fprintln(console.output, "Отменено.")
			return
		}
	}
	noteSession(root, vm.Name, target, project, console)
	if err := runManage(root, target, environment); err != nil {
		fmt.Fprintln(console.errors, err)
	}
}

// createVM спрашивает имя и бюджет нового гостя и запускает подготовку.
func createVM(root string, console streams) {
	reader := console.input
	fmt.Fprint(console.output, "Имя новой VM (например vm-03): ")
	name, err := reader.ReadString('\n')
	if err != nil && strings.TrimSpace(name) == "" {
		return
	}
	name = strings.TrimSpace(name)
	if !validName(name) {
		fmt.Fprintln(console.errors, "Имя VM задаётся как vm-01, номера от 01 до 254.")
		return
	}
	environment := map[string]string{"VM": name}
	for _, field := range []struct{ prompt, key string }{
		{"CPU", "VCPU"}, {"RAM, МиБ", "RAM_MB"}, {"Диск, ГиБ", "DISK_GB"},
	} {
		fmt.Fprintf(console.output, "%s (Enter — значение из .env): ", field.prompt)
		answer, _ := reader.ReadString('\n')
		answer = strings.TrimSpace(answer)
		if answer == "" {
			continue
		}
		value, err := strconv.Atoi(answer)
		if err != nil || value <= 0 {
			fmt.Fprintln(console.errors, "Ожидалось положительное число.")
			return
		}
		environment[field.key] = answer
	}
	if err := runManage(root, "vm-setup", environment); err != nil {
		fmt.Fprintln(console.errors, err)
	}
}
