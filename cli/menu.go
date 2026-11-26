package main

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// terminal переключает режим ввода через stty: меню читает отдельные
// клавиши, а запущенные из него команды получают обычный терминал.
type terminal struct {
	saved     string
	screen    io.Writer
	alternate bool
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

// drain выбрасывает ввод, накопившийся за время работы команды. Пока список
// ждал Enter, лишние клавиши доставались этому ожиданию; без паузы их никто
// не читает, и нажатое в консоли гостя после Ctrl+] сработало бы действием
// над гостем. Отказ stty здесь безвреден: тогда ввод просто остаётся как был.
func (console *terminal) drain() {
	if _, err := stty("min", "0", "time", "0"); err != nil {
		return
	}
	defer stty("min", "1", "time", "0")
	buffer := make([]byte, 4096)
	for {
		count, err := os.Stdin.Read(buffer)
		if err != nil || count == 0 {
			return
		}
	}
}

func (console *terminal) cooked() {
	if console.saved != "" {
		stty(strings.Fields(console.saved)...)
	}
}

func (console *terminal) enterScreen() {
	if console.screen != nil && !console.alternate {
		fmt.Fprint(console.screen, "\x1b[?1049h")
		console.alternate = true
	}
}

func (console *terminal) leaveScreen() {
	if console.alternate {
		fmt.Fprint(console.screen, "\x1b[?1049l")
		console.alternate = false
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
// самого терминала, иначе atrice </dev/null падал бы вместо показа справки.
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
	buffer := make([]byte, 4096)
	count, err := os.Stdin.Read(buffer)
	if err != nil {
		return nil, err
	}
	if count == 0 {
		return nil, io.EOF
	}
	return buffer[:count], nil
}

// readMenuKey читает очередную клавишу экранов списка и сессий. Отдельная
// переменная нужна тестам: они подают последовательность клавиш без tty.
var readMenuKey = readKey

// readKey читает одну клавишу. Разбор отдан decodeKey: клавиша приводится к
// руне, поэтому действия вызываются и с русской раскладки.
func readKey() (rune, error) {
	data, err := readBytes()
	if err != nil {
		return 0, err
	}
	return decodeKey(data), nil
}

// sessionTargets — цели, после которых список открывается сразу, без запроса
// Enter. Из консоли гостя и из агента выходят по Ctrl+], и ещё одно нажатие
// мешало бы сразу перейти к следующему гостю. Вывод читающих целей, наоборот,
// нужно успеть прочитать, поэтому для них пауза остаётся.
var sessionTargets = map[string]bool{"vm-console": true, "claude": true,
	"vm-session-open": true, "vm-session-claude": true,
	"vm-session-new": true, "vm-session-direct": true}

type menuInventory struct {
	vms []VM
	err error
}

type menuKey struct {
	key rune
	err error
}

func fetchMenuInventory(root string, console streams) <-chan menuInventory {
	result := make(chan menuInventory, 1)
	go func() {
		vms, err := inventory(root, console)
		result <- menuInventory{vms: vms, err: err}
	}()
	return result
}

func fetchMenuKey() <-chan menuKey {
	result := make(chan menuKey, 1)
	go func() {
		key, err := readMenuKey()
		result <- menuKey{key: key, err: err}
	}()
	return result
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
	if file, ok := console.output.(*os.File); ok && os.Getenv("TERM") != "dumb" {
		if info, err := file.Stat(); err == nil && info.Mode()&os.ModeCharDevice != 0 {
			screen.screen = file
		}
	}
	screen.enterScreen()
	defer screen.leaveScreen()
	// Пока открыто меню, сообщения о недоступном docker не портят кадр:
	// их место занимает строка состояния под списком.
	quiet := streams{input: console.input, output: console.output, errors: io.Discard}
	cursor := 0
	chosen := ""
	notice := ""
	vms, err := loadVMs(stateRoot(root))
	if err != nil {
		notice = err.Error()
	}
	chosen = selectedName(vms, cursor)
	refresh := true
	forceRefresh := false
	var pending <-chan menuInventory
	var input <-chan menuKey
	for {
		if refresh && pending == nil {
			if forceRefresh {
				channelCache.clear()
				forceRefresh = false
			}
			pending = fetchMenuInventory(root, quiet)
			refresh = false
		}
		height, width := terminalSize()
		status := notice
		if status == "" && pending != nil {
			status = "Обновляются статусы ВМ и сессий..."
		}
		fmt.Fprint(console.output, "\x1b[2J\x1b[H"+
			renderMenu(vms, cursor, width, height, root, status, time.Now()))
		if input == nil {
			input = fetchMenuKey()
		}
		var key rune
		select {
		case snapshot := <-pending:
			pending = nil
			if !refresh {
				if snapshot.err != nil {
					notice = snapshot.err.Error()
				} else {
					vms = snapshot.vms
					cursor = selectVM(vms, chosen, cursor)
					chosen = selectedName(vms, cursor)
				}
			}
			continue
		case event := <-input:
			input = nil
			if event.err != nil {
				return 0
			}
			key = event.key
		}
		notice = ""
		switch navigation(key) {
		case actionQuit:
			return 0
		case actionUp:
			cursor = moveCursor(cursor, -1, len(vms))
			chosen = selectedName(vms, cursor)
			continue
		case actionDown:
			cursor = moveCursor(cursor, 1, len(vms))
			chosen = selectedName(vms, cursor)
			continue
		case actionRefresh:
			// Обновление спрашивает канал заново, не дожидаясь срока.
			forceRefresh = true
			refresh = true
			continue
		case actionCreate:
			refresh = true
			if err := outside(screen, console, "", func() bool { createVM(root, console); return true }); err != nil {
				fmt.Fprintln(console.errors, err)
				return 1
			}
			continue
		}
		target, known := keyActions[key]
		if !known && key != keyEnter {
			continue
		}
		if len(vms) == 0 {
			notice = "сначала создайте гостя клавишей n"
			continue
		}
		refresh = true
		selected := vms[cursor]
		chosen = selected.Name
		if key != keyEnter {
			fresh, found := menuVM(root, chosen, quiet, false)
			if !found {
				notice = chosen + " больше нет в списке"
				continue
			}
			selected = fresh
		}
		project := ""
		session := ""
		if key == keyEnter {
			// Enter — подтверждение подсвеченного пункта, и только оно: на
			// списке ВМ он выбирает ВМ и показывает её живые сессии. Ни ВМ,
			// ни агента Enter сам не запускает и оболочку не открывает —
			// вслепую запущенный агент получал отказ занятой оболочки, и
			// вход срывался именно так.
			chosenTarget, chosenSession, err := sessionsScreen(root, selected.Name, console, quiet)
			if err != nil {
				fmt.Fprintln(console.errors, err)
				return 1
			}
			if chosenTarget == "" {
				continue
			}
			target, session = chosenTarget, chosenSession
			if fresh, found := menuVM(root, selected.Name, quiet, false); found {
				selected = fresh
			}
		}
		// Обычная консоль и агент запрашиваются явно, поэтому отказ лучше
		// показать строкой состояния, не уводя экран.
		trouble := ""
		switch target {
		case "claude-auth":
			if !selected.Configured() {
				trouble = "Сначала настройте ВМ клавишей e."
			}
		case "vm-console", "vm-session-open":
			trouble = consoleTrouble(selected)
		case "claude":
			trouble = claudeTrouble(selected)
		case "vm-session-claude":
			trouble = newSessionTrouble(selected)
		case "vm-session-new", "vm-session-direct":
			// Прямой путь не касается serial-оболочки, поэтому её занятость
			// здесь ничего не запрещает: проверяются только настройка и запуск.
			trouble = consoleTrouble(selected)
		}
		if trouble != "" {
			notice = trouble
			continue
		}
		ok := true
		if err := outside(screen, console, target, func() bool {
			ok = menuTarget(root, target, selected, project, session, console)
			return ok
		}); err != nil {
			fmt.Fprintln(console.errors, err)
			return 1
		}
		if !ok {
			notice = "команда завершилась отказом, сообщение выше"
		}
	}
}

// sessionsScreen показывает живые сессии выбранного гостя и возвращает цель
// manage.sh вместе с выбранной сессией, или пустую цель, если пользователь
// вернулся в список. Данные только из канала: экран перечитывает их при
// входе и по r.
//
// Enter здесь подтверждает подсвеченную сессию и подключается к ней целью
// vm-session-open, которая несёт имя сессии в RESUME. Пустой список
// подтверждать нечего, и Enter тогда не делает ничего: ни ВМ, ни агента он не
// запускает. Обычная консоль осталась отдельным пунктом — клавишей c, —
// поэтому Enter сам по себе оболочку не открывает.
func sessionsScreen(root, name string, console, quiet streams) (string, string, error) {
	cursor := 0
	chosen := ""
	notice := ""
	var vm VM
	var sessions []LiveSession
	refresh := true
	for {
		if refresh {
			fresh, found := namedVM(root, name, quiet)
			if !found {
				return "", "", nil
			}
			vm = fresh
			sessions = liveSessions(vm)
			cursor = selectSession(sessions, chosen, cursor)
			chosen = selectedSession(sessions, cursor)
			refresh = false
		}
		height, width := terminalSize()
		fmt.Fprint(console.output, "\x1b[2J\x1b[H"+
			renderSessions(vm, cursor, notice, width, height, time.Now()))
		key, err := readMenuKey()
		if err != nil {
			return "", "", err
		}
		notice = ""
		switch key {
		case 'q', keyEscape, keyDetach, keyInterrupt, keyEndOfStream:
			return "", "", nil
		case 'k', keyUp:
			cursor = moveCursor(cursor, -1, len(sessions))
			chosen = selectedSession(sessions, cursor)
		case 'j', keyDown:
			cursor = moveCursor(cursor, 1, len(sessions))
			chosen = selectedSession(sessions, cursor)
		case 'r':
			channelCache.clear()
			refresh = true
		case keyEnter:
			if chosen == "" {
				notice = "живых сессий нет: новую создаёт a, обычную консоль открывает c"
				continue
			}
			fresh, found := namedVM(root, name, quiet)
			if !found {
				return "", "", nil
			}
			vm = fresh
			sessions = liveSessions(vm)
			cursor = selectSession(sessions, chosen, cursor)
			if selectedSession(sessions, cursor) != chosen {
				notice = "выбранная сессия завершилась"
				chosen = selectedSession(sessions, cursor)
				continue
			}
			// Подтверждение подсвеченной сессии. К уже подключённой сессии
			// возвращаются обычной консолью: её клиент tmux работает в консоли
			// гостя и занимает оболочку, поэтому attach в неё отправить нельзя
			// — байты ушли бы этому клиенту как нажатия. Так же выглядит и
			// возврат после Ctrl+] или закрытия терминала: клиент в госте
			// остался подключённым.
			// Прямым путём открывается ровно подсвеченная сессия, даже
			// подключённая: клиент tmux здесь свой, serial не затрагивается.
			if vm.Live.directWorks() {
				return "vm-session-direct", chosen, nil
			}
			if attached(sessions, cursor) {
				return "vm-console", "", nil
			}
			return "vm-session-open", chosen, nil
		case 'c':
			return "vm-console", "", nil
		case 'a':
			// Новая сессия рядом с живыми, а не вход в существующую: имя ей
			// даёт manage.sh, поэтому в RESUME отсюда ничего не уходит.
			// Прямым путём это работает и при занятой оболочке, иначе
			// остаётся путь через консоль с его отказом при занятости.
			if vm.Live.directWorks() {
				return "vm-session-new", "", nil
			}
			return "vm-session-claude", "", nil
		}
	}
}

// namedVM перечитывает состояние одного гостя вместе с отчётом канала.
func namedVM(root, name string, quiet streams) (VM, bool) {
	return menuVM(root, name, quiet, true)
}

func menuVM(root, name string, quiet streams, probe bool) (VM, bool) {
	vms, err := inventoryStates(root, quiet)
	if err != nil {
		return VM{}, false
	}
	for index := range vms {
		vm := &vms[index]
		if vm.Name != name {
			continue
		}
		now := time.Now()
		if probe {
			applyLive(root, vms[index:index+1], channelCache, now)
		} else if vm.Running() {
			vm.Live, _ = channelCache.lookup(root, name, now)
		}
		return *vm, true
	}
	return VM{}, false
}

// outside выполняет действие в обычном режиме терминала и возвращает меню
// в неформатированный режим. Если вернуть режим не удалось, меню закрывается:
// продолжать в построчном вводе означало бы не реагировать на клавиши.
// Решение о паузе принимается после работы body: до неё неизвестно, удалась
// цель или отказала, а отказ обязан остаться на экране.
func outside(screen *terminal, console streams, target string, body func() bool) error {
	if target == "vm-git-status" && screen.alternate {
		// Форма Git остаётся на экране меню, чтобы не дописывать запросы
		// поверх прежнего вывода консоли гостя.
		fmt.Fprint(screen.screen, "\x1b[2J\x1b[H")
	} else {
		screen.leaveScreen()
	}
	screen.cooked()
	pause := pauseAfter(target, body())
	if pause {
		fmt.Fprint(console.output, "\nНажмите Enter, чтобы вернуться в список. ")
		console.input.ReadString('\n')
	}
	if err := screen.raw(); err != nil {
		return err
	}
	if !pause {
		screen.drain()
	}
	screen.enterScreen()
	return nil
}

// menuTarget запускает выбранное действие над гостем и сообщает, удалось ли
// оно: от этого зависит, задержать ли экран с сообщением об отказе.
func menuTarget(root, target string, vm VM, project, session string, console streams) bool {
	environment := map[string]string{"VM": vm.Name}
	if project != "" {
		environment["PROJECT"] = project
	}
	// Имя живой сессии идёт тем же RESUME, которым пользуется claude --resume:
	// другого канала для него в manage.sh нет.
	if session != "" {
		environment["RESUME"] = session
	}
	switch target {
	case "alias":
		alias, err := promptAlias(console, "Имя для списка (Enter сбрасывает имя) ")
		if err == nil {
			err = os.WriteFile(filepath.Join(vm.Dir, "alias"), []byte(alias+"\n"), 0600)
		}
		if err != nil {
			fmt.Fprintln(console.errors, err)
			return false
		}
		return true
	case "vm-setup":
		if vm.Configured() {
			target = "vm-proxy"
		}
	case "vm-git-status":
		if err := runManage(root, target, environment); err != nil {
			fmt.Fprintln(console.errors, err)
			return false
		}
		if _, err := os.Stat(filepath.Join(vm.Dir, "git-host-public", "current")); err == nil {
			fmt.Fprint(console.output, "\nПроверить, что видно агенту в Git (Enter — проверить, q — назад) ")
			answer, err := console.input.ReadString('\n')
			if err == nil && strings.TrimSpace(answer) == "" {
				if err := runManage(root, "vm-git-audit", environment); err != nil {
					fmt.Fprintln(console.errors, err)
					return false
				}
			}
			return true
		}
		return configureGitInMenu(root, vm.Name, console)
	case "vm-logs":
		environment["LOG_TAIL"] = "50"
		environment["LOG_SERVICE"] = ""
	case "vm-remove":
		// Диск гостя из меню не удаляется: для этого есть atrice rm --delete-disk.
		environment["DISK_KEEP"] = "1"
		prompt := "Будут удалены контейнеры " + vm.Name + ". Диск и настройки сохранятся, ВМ исчезнет из списка."
		if !confirmed(prompt, vm.Name, console) {
			fmt.Fprintln(console.output, "Отменено.")
			return true
		}
	}
	if err := runManage(root, target, environment); err != nil {
		fmt.Fprintln(console.errors, err)
		return false
	}
	noteSession(root, vm.Name, target, project, console)
	return true
}

func configureGitInMenu(root, vm string, console streams) bool {
	prompt := func(label string) (string, error) {
		fmt.Fprint(console.output, label)
		line, err := console.input.ReadString('\n')
		return strings.TrimSpace(line), err
	}
	repo, err := prompt("\nРепозиторий для новой настройки (Enter для возврата) ")
	if err != nil || repo == "" {
		return true
	}
	provider, err := prompt("Провайдер github или gitlab (по умолчанию github) ")
	if err != nil {
		return true
	}
	options := []string{vm, "--repo", repo}
	if provider != "" {
		options = append(options, "--provider", provider)
	}
	if provider == "gitlab" {
		url, err := prompt("HTTPS-адрес GitLab ")
		if err != nil {
			return true
		}
		options = append(options, "--url", url)
	}
	branches, err := prompt("Ветки чтения, через запятую (например reference=release/test) ")
	if err != nil {
		return true
	}
	if branches != "" {
		for _, branch := range strings.Split(branches, ",") {
			options = append(options, "--read-branch", strings.TrimSpace(branch))
		}
	}
	write, err := prompt("Ветка записи (Enter для режима чтения) ")
	if err != nil {
		return true
	}
	if write != "" {
		options = append(options, "--write", write)
	}
	action, err := parseGit(options)
	if err == nil {
		err = runManage(root, action.target, action.environment)
	}
	if err != nil {
		fmt.Fprintln(console.errors, err)
		return false
	}
	return true
}

func promptAlias(console streams, prompt string) (string, error) {
	fmt.Fprint(console.output, prompt)
	answer, err := console.input.ReadString('\n')
	if err != nil {
		return "", err
	}
	alias := strings.TrimSpace(answer)
	if !validAlias(alias) {
		return "", fmt.Errorf("Имя должно содержать до 32 печатных символов.")
	}
	return alias, nil
}

// createVM спрашивает идентификатор, имя и бюджет нового гостя и запускает подготовку.
func createVM(root string, console streams) {
	reader := console.input
	fmt.Fprint(console.output, "Технический ID ВМ (Enter выберет свободный) ")
	name, err := reader.ReadString('\n')
	if err != nil && strings.TrimSpace(name) == "" {
		return
	}
	name = strings.TrimSpace(name)
	if name == "" {
		name, err = nextVMName(stateRoot(root))
		if err != nil {
			fmt.Fprintln(console.errors, err)
			return
		}
		fmt.Fprintln(console.output, name)
	}
	if !validName(name) {
		fmt.Fprintln(console.errors, "Имя VM задаётся как vm-01, номера от 01 до 254.")
		return
	}
	dir := filepath.Join(stateRoot(root), name)
	if _, err := os.Lstat(dir); !os.IsNotExist(err) {
		fmt.Fprintln(console.errors, "Идентификатор занят. Для настройки ВМ в списке нажмите e.")
		return
	}
	alias, err := promptAlias(console, "Имя для списка (Enter пропускает имя) ")
	if err != nil {
		fmt.Fprintln(console.errors, err)
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
	setupErr := runManage(root, "vm-setup", environment)
	if alias != "" {
		if _, err := os.Stat(dir); err == nil {
			if err := os.WriteFile(filepath.Join(dir, "alias"), []byte(alias+"\n"), 0600); err != nil {
				fmt.Fprintln(console.errors, err)
			}
		}
	}
	if setupErr != nil {
		fmt.Fprintln(console.errors, setupErr)
	}
}
