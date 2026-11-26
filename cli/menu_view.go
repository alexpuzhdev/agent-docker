package main

import (
	"strings"
	"time"
)

// hints — подсказки по клавишам, по две строки, чтобы помещаться в узкий
// терминал. Буквы названы латиницей, но нажимается просто та же клавиша:
// русская раскладка сворачивается по расположению, и «с» работает как c.
var hints = []string{
	"Enter выбрать ВМ: её живые сессии   c оболочка   a Claude   u запустить",
	"n создать   e настройка ВМ/прокси   m имя   i вход Claude   g git",
	"r обновить   s остановить   l логи   p профиль   d удалить   q выход",
}

// sessionHints — подсказки экрана живых сессий одного гостя. Enter здесь
// только подтверждает подсвеченную сессию; обычная консоль и запуск агента
// остаются отдельными пунктами и вызываются своими клавишами.
var sessionHints = []string{
	"Enter подключиться к сессии   a новая сессия Claude   c оболочка",
	"r обновить   Ctrl+], Esc или q — назад в список",
}

// sessionAttachNote объясняет, почему к подключённой сессии возвращаются
// консолью: её клиент работает в консоли гостя и оболочку не отпускает.
const sessionAttachNote = "подсвеченная сессия уже подключена: Enter вернёт в неё консолью"

// menuAction — действие, выбранное клавишей в меню.
type menuAction string

const (
	actionNone    menuAction = ""
	actionQuit    menuAction = "quit"
	actionUp      menuAction = "up"
	actionDown    menuAction = "down"
	actionRefresh menuAction = "refresh"
	actionCreate  menuAction = "create"
)

// keyActions — клавиши, которым соответствует цель manage.sh. Ключи —
// латинские буквы нижнего регистра: клавиша приводится к ним до поиска.
var keyActions = map[rune]string{
	'c': "vm-console",
	'a': "claude",
	'u': "vm-up",
	's': "vm-stop",
	'g': "vm-git-status",
	'e': "vm-setup",
	'm': "alias",
	'i': "claude-auth",
	'l': "vm-logs",
	'p': "vm-profile",
	'd': "vm-remove",
}

// navigation переводит нажатую клавишу в действие навигации.
func navigation(key rune) menuAction {
	switch key {
	case 'q', keyInterrupt, keyEndOfStream:
		return actionQuit
	case 'k', keyUp:
		return actionUp
	case 'j', keyDown:
		return actionDown
	case 'r':
		return actionRefresh
	case 'n':
		return actionCreate
	}
	return actionNone
}

// moveCursor сдвигает курсор, останавливаясь на границах списка.
func moveCursor(current, delta, count int) int {
	if count == 0 {
		return 0
	}
	next := current + delta
	if next < 0 {
		return 0
	}
	if next >= count {
		return count - 1
	}
	return next
}

// selectVM находит строку гостя по имени. Выбор держится за имя, а не за
// номер строки: после создания, удаления или переименования гостя тот же
// номер указывал бы на другого, и возврат из консоли терял бы выбор.
func selectVM(vms []VM, name string, fallback int) int {
	for index, vm := range vms {
		if vm.Name == name {
			return index
		}
	}
	// Выбранного гостя в списке больше нет: курсор остаётся на том же месте,
	// прижатый к границам нового списка.
	return moveCursor(fallback, 0, len(vms))
}

// selectedName возвращает имя гостя под курсором; у пустого списка имени нет.
func selectedName(vms []VM, cursor int) string {
	if cursor < 0 || cursor >= len(vms) {
		return ""
	}
	return vms[cursor].Name
}

// consoleTrouble объясняет, почему обычную serial-консоль гостя сейчас
// открыть нельзя, или возвращает пустую строку, если открывать можно.
// Проверяются только настройка и запуск: консоль обязана работать и на
// старом образе, поэтому ни tmux, ни канал сессий, ни наличие агента здесь
// не учитываются. Неизвестное состояние контейнеров попыткой не считается
// запретом: отказ придёт от manage.sh и останется на экране.
func consoleTrouble(vm VM) string {
	if !vm.Configured() {
		return vm.Name + " ещё не настроена: atrice new " + vm.Name
	}
	if vm.StatusKnown && !vm.Running() {
		return vm.Name + " остановлена: сначала atrice up " + vm.Name
	}
	return ""
}

// claudeTrouble объясняет, почему запуск агента сейчас не пройдёт, или
// возвращает пустую строку. Решение принимается только по тому, что хост
// знает точно: настройка гостя и занятость консоли из отчёта канала.
// Живые сессии запуску не мешают — отсоединённый tmux консоль не занимает, —
// поэтому по ним ничего не запрещается: отказ придёт от гостя и останется
// на экране.
func claudeTrouble(vm VM) string {
	if !vm.Configured() {
		return vm.Name + " ещё не настроена: atrice new " + vm.Name
	}
	if vm.Live.consoleBusy() {
		return vm.Name + ": консоль гостя уже открыта, сначала выйдите из неё по Ctrl+]"
	}
	if trouble := shellTrouble(vm); trouble != "" {
		return trouble
	}
	return ""
}

// newSessionTrouble объясняет, почему новую сессию сейчас не создать, или
// возвращает пустую строку. Создание отправляет команду в оболочку гостя,
// поэтому занятая консоль — запрет: иначе команда ушла бы работающему в ней
// процессу как нажатия клавиш. Живые сессии сами по себе не мешают —
// отсоединённый tmux оболочку не занимает.
func newSessionTrouble(vm VM) string {
	if !vm.Configured() {
		return vm.Name + " ещё не настроена: atrice new " + vm.Name
	}
	if vm.StatusKnown && !vm.Running() {
		return vm.Name + " остановлена: сначала atrice up " + vm.Name
	}
	if vm.Live.consoleBusy() {
		return vm.Name + ": консоль гостя занята, выйдите из сессии в ней и повторите"
	}
	if trouble := shellTrouble(vm); trouble != "" {
		return trouble
	}
	return ""
}

// shellTrouble объясняет занятую serial-оболочку гостя и называет безопасный
// выход. Так выглядит гость, в котором приложение запустили прямо в консоли:
// консоль свободна, живых сессий канал не видит, но оболочка занята, и
// отправить в неё команду нельзя — она ушла бы приложению как нажатия.
// Завершать его за пользователя нельзя, поэтому путь один: войти обычной
// консолью и выйти из приложения штатно.
func shellTrouble(vm VM) string {
	if !vm.Live.shellWorking() {
		return ""
	}
	return vm.Name + ": на serial-оболочке работает приложение — войдите по c, " +
		"завершите его штатно, и оболочка вернёт приглашение"
}

// liveStateLine — строка состояния канала для экрана сессий. Состояние и
// подсказка показываются как есть: подсказку пишет сам канал, и у каждого
// состояния она своя.
func liveStateLine(vm VM) string {
	if vm.Live == nil {
		if vm.StatusKnown && !vm.Running() {
			return "канал не спрашивали: " + vm.Name + " остановлена, живых сессий нет"
		}
		return "канал не спрашивали"
	}
	line := "состояние канала: " + vm.Live.State
	// Пустой список — не доказательство того, что в госте ничего нет: агент,
	// запущенный прямо в serial-оболочке, сессией tmux не является и каналу не
	// виден. Состояние оболочки приходит отдельной читающей целью и говорит об
	// этом прямо, не выдумывая сессий.
	switch {
	case vm.Live.shellWorking():
		line += "; на serial-оболочке работает приложение"
	case vm.Live.Shell == shellReady:
		line += "; оболочка гостя ждёт ввод"
	}
	if vm.Live.directWorks() {
		// Прямой путь независим от serial: Enter и a работают и при занятой
		// оболочке, поэтому про это стоит сказать прямо.
		line += "; прямой вход в сессии доступен"
	}
	return line
}

// liveSessions — живые сессии гостя из отчёта канала. Пустой список значит,
// что подтверждать Enter нечего: ни ВМ, ни Claude он сам не запускает.
func liveSessions(vm VM) []LiveSession {
	if vm.Live == nil {
		return nil
	}
	return vm.Live.Sessions
}

// selectSession держит выбор за именем сессии: список перечитывается из
// канала, и после обновления тот же номер строки указывал бы на другую
// сессию, а подтверждение Enter открывало бы не ту.
func selectSession(sessions []LiveSession, name string, fallback int) int {
	for index, session := range sessions {
		if session.Name == name {
			return index
		}
	}
	return moveCursor(fallback, 0, len(sessions))
}

// attached сообщает, подключён ли к подсвеченной сессии клиент. Подключённая
// сессия занимает оболочку гостя, и вернуться в неё можно только консолью.
func attached(sessions []LiveSession, cursor int) bool {
	return cursor >= 0 && cursor < len(sessions) && sessions[cursor].Attached
}

// selectedSession возвращает имя подсвеченной сессии; у пустого списка
// подсвечивать нечего, и имени нет.
func selectedSession(sessions []LiveSession, cursor int) string {
	if cursor < 0 || cursor >= len(sessions) {
		return ""
	}
	return sessions[cursor].Name
}

// sessionRows собирает таблицу живых сессий гостя.
func sessionRows(vm VM, now time.Time) [][]string {
	if vm.Live == nil {
		return nil
	}
	rows := make([][]string, 0, len(vm.Live.Sessions))
	for _, session := range vm.Live.Sessions {
		attached := "нет"
		if session.Attached {
			attached = colors.good("да")
		}
		age := humanAge(time.Unix(session.Created, 0).UTC(), now)
		if session.Created == 0 {
			age = "—"
		}
		command := session.Command
		if command == "" {
			command = "—"
		}
		rows = append(rows, []string{colors.accent(session.Name), command, attached, age})
	}
	return rows
}

// sessionHeaders — заголовки таблицы живых сессий.
var sessionHeaders = []string{"СЕССИЯ", "КОМАНДА", "ПОДКЛЮЧЕНА", "СОЗДАНА"}

// renderSessions рисует экран живых сессий одного гостя. Всё на нём приходит
// из канала: состояние и подсказка — как есть, список — таблицей с курсором.
// Старый образ, пустой список и отказ канала различаются текстом состояния, а
// не одинаково пустой таблицей.
func renderSessions(vm VM, cursor int, notice string, width, height int, now time.Time) string {
	if width < 20 {
		width = 20
	}
	lines := []string{
		truncate(colors.accent("atrice "+version+" — "+vm.Name+": живые сессии"), width),
		truncate(colors.muted(liveStateLine(vm)), width),
	}
	if vm.Live != nil && vm.Live.Hint != "" {
		lines = append(lines, truncate(colors.muted(vm.Live.Hint), width))
	}
	if vm.Live != nil && vm.Live.Detail != "" {
		lines = append(lines, truncate(colors.muted(vm.Live.Detail), width))
	}
	lines = append(lines, "")
	rows := sessionRows(vm, now)
	if len(rows) == 0 {
		lines = append(lines, truncate("  "+liveText(vm), width))
	} else {
		table := strings.Split(strings.TrimRight(renderTable(sessionHeaders, rows), "\n"), "\n")
		lines = append(lines, truncate("  "+table[0], width))
		for index, row := range table[1:] {
			prefix := "  "
			if index == cursor {
				prefix = colors.accent("> ")
			}
			lines = append(lines, truncate(prefix+row, width))
		}
	}
	if attached(liveSessions(vm), cursor) {
		lines = append(lines, "", truncate(colors.muted(sessionAttachNote), width))
	}
	if vm.Live != nil && vm.Live.consoleBusy() {
		lines = append(lines, "", truncate(colors.muted("консоль гостя сейчас занята"), width))
	}
	lines = append(lines, "")
	for _, hint := range sessionHints {
		lines = append(lines, truncate(colors.muted(hint), width))
	}
	lines = append(lines, truncate(notice, width))
	// Низкое окно обрезается: прокрутка сдвинула бы состояние канала за край.
	if height > 0 && len(lines) > height {
		lines = lines[:height]
	}
	return strings.Join(lines, "\r\n")
}

// pauseAfter решает, задержать ли экран после выполнения цели. Удачную
// сессию задерживать нечем: из неё выходят по Ctrl+], и список открывается
// сразу. Отказ, наоборот, остаётся на экране — иначе сообщение о занятой
// консоли или несовместимом образе исчезло бы вместе с кадром списка.
func pauseAfter(target string, ok bool) bool {
	return !sessionTargets[target] || !ok
}

// windowStart выбирает начало видимого окна так, чтобы курсор оставался внутри.
func windowStart(cursor, visible, count int) int {
	if visible <= 0 || count <= visible {
		return 0
	}
	start := cursor - visible/2
	if start < 0 {
		start = 0
	}
	if start > count-visible {
		start = count - visible
	}
	return start
}

// renderMenu строит кадр меню. Строки разделяются \r\n: терминал находится в
// неформатированном режиме и сам каретку не возвращает.
func renderMenu(vms []VM, cursor, width, height int, root, notice string, now time.Time) string {
	if width < 20 {
		width = 20
	}
	rows := make([][]string, 0, len(vms))
	for _, vm := range vms {
		rows = append(rows, vmRow(vm, now))
	}
	table := strings.Split(strings.TrimRight(renderTable(listHeaders, rows), "\n"), "\n")
	header := table[0]
	body := table[1:]
	if len(vms) == 0 {
		body = []string{"гостей нет, создайте первого клавишей n"}
	}
	// Кадр занимает ровно height строк: заголовок, корень, пустая строка,
	// шапка таблицы, список, пустая строка, подсказки и строка сообщения.
	visible := height - (6 + len(hints))
	if visible < 1 {
		visible = 1
	}
	start := windowStart(cursor, visible, len(body))
	lines := []string{
		truncate(colors.accent("atrice "+version+" — agent-docker"), width),
		truncate(colors.muted(root), width),
		"",
		truncate("  "+header, width),
	}
	for index := start; index < len(body) && index < start+visible; index++ {
		prefix := "  "
		if index == cursor && len(vms) > 0 {
			prefix = colors.accent("> ")
		}
		lines = append(lines, truncate(prefix+body[index], width))
	}
	lines = append(lines, "")
	for _, hint := range hints {
		lines = append(lines, truncate(colors.muted(hint), width))
	}
	lines = append(lines, truncate(notice, width))
	// В очень низком окне кадр обрезается: прокрутка терминала сдвинула бы
	// верх списка за край экрана.
	if len(lines) > height {
		lines = lines[:height]
	}
	return strings.Join(lines, "\r\n")
}
