package main

import (
	"encoding/json"
	"errors"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Живые сессии гостя приходят только из канала сессий: цель
// vm-sessions-check отдаёт одну строку JSON, которую внутри гостя печатает
// sessions.py diagnose. Память CLI о прошлых входах здесь не участвует —
// запись о давнем входе выглядела бы живой сессией, хотя в госте уже ничего
// не работает.
//
// Отчёт — данные недоверенного гостя: строки чистятся и обрезаются, ничего
// из них не исполняется. Состояния не сводятся к паре «есть сессии или
// нет»: старый образ, пустой список и отказ канала — разные вещи, и
// подсказки у них разные. Незнакомое состояние показывается как есть: поле
// state расширяемо в рамках api = 1.
const (
	liveLive         = "live"
	liveEmpty        = "empty"
	liveOldImage     = "old-image"
	liveNoService    = "no-service"
	liveNoTmux       = "no-tmux"
	liveNoPermission = "no-permission"
	liveUnparsed     = "format-unparsed"
	liveSlow         = "slow"
	liveBroken       = "broken"
	// liveNoReport — состояние хоста, а не гостя: опрос не состоялся или
	// ответ не разобран. Оно не подменяет ни пустой список, ни старый образ:
	// про сессии в этом случае неизвестно ничего.
	liveNoReport = "no-report"
)

// liveAPI — версия контракта канала, другую версию хост не разбирает.
const liveAPI = 1

// Пределы отчёта. Список сессий одного гостя короткий, и всё, что больше,
// означает неисправный или враждебный ответ.
const (
	liveFieldLimit   = 160
	liveSessionLimit = 256
	liveReplyLimit   = 128 * 1024
	liveShown        = 2
)

// liveTTL задаёт время жизни отчёта после завершения опроса.
const liveTTL = 5 * time.Second

// liveTimeout — предел на один опрос. Срок у самого канала свой и короткий,
// здесь ограничивается вся цель вместе с docker compose exec.
const liveTimeout = 30 * time.Second

// LiveSession — одна живая сессия tmux из отчёта канала.
type LiveSession struct {
	Name     string
	Created  int64
	Attached bool
	Command  string
}

// Live — отчёт канала сессий одного гостя вместе с состоянием его
// serial-оболочки. Оболочка приходит отдельной читающей целью
// vm-shell-check, а не каналом: канал по контракту журнала консоли не
// касается, и это закреплено его тестом.
type Live struct {
	State    string
	Hint     string
	Detail   string
	Console  string
	Shell    string
	Direct   string
	Skipped  int
	Sessions []LiveSession
}

// Состояния serial-оболочки гостя из цели vm-shell-check.
const (
	shellReady   = "ready"
	shellBusy    = "busy"
	shellUnknown = "unknown"
)

// Состояния прямого пути в сессии из цели vm-session-direct-check. Путь
// независим от serial-консоли: он идёт отдельным портом virtio-serial и своим
// клиентом tmux в госте, поэтому работает и при занятой оболочке. Его может
// не быть вовсе (порт появляется при следующем запуске ВМ) или служба может
// быть не установлена — это разные состояния, и ни одно не подменяет другое.
const (
	directReady   = "ready"
	directSilent  = "silent"
	directAbsent  = "absent"
	directUnknown = "unknown"
)

// liveTexts — короткие названия состояний для колонки списка. Текста нет
// только у live: там на месте состояния перечисляются сами сессии.
var liveTexts = map[string]string{
	liveEmpty:        "сессий нет",
	liveOldImage:     "старый образ",
	liveNoTmux:       "нет tmux",
	liveNoService:    "служба не ответила",
	liveNoPermission: "нет доступа к tmux",
	liveUnparsed:     "формат не разобран",
	liveSlow:         "канал не успел",
	liveBroken:       "ответ испорчен",
	liveNoReport:     "опрос не удался",
}

// liveFailures — состояния, которые означают неисправность канала, а не
// ответ о сессиях. Они показываются красным: пустой список и старый образ —
// обычная жизнь, а эти требуют вмешательства.
var liveFailures = map[string]bool{
	liveNoService: true, liveNoPermission: true, liveUnparsed: true,
	liveSlow: true, liveBroken: true, liveNoReport: true,
}

// liveWorks сообщает, что канал ответил о сессиях: про них известно точно.
// Старый образ, отсутствие tmux и любой отказ сюда не попадают.
func (report *Live) liveWorks() bool {
	return report != nil && (report.State == liveLive || report.State == liveEmpty)
}

// consoleBusy сообщает, что пользовательская консоль гостя сейчас занята.
// Это отдельная отметка, а не запрет: клавиша c по ней не блокируется.
func (report *Live) consoleBusy() bool {
	return report != nil && report.Console == "busy"
}

// shellWorking сообщает, что на serial-оболочке гостя работает приложение
// переднего плана: отправлять в неё команды нельзя, а запуск агента и
// подключение к сессии откажут, пока оно не завершится.
func (report *Live) shellWorking() bool {
	return report != nil && report.Shell == shellBusy
}

// directReady сообщает, что прямой вход в сессии доступен: создать и открыть
// сессию можно, не касаясь serial-оболочки.
func (report *Live) directWorks() bool {
	return report != nil && report.Direct == directReady
}

// liveField приводит строку отчёта к безопасному для терминала виду.
func liveField(value string) string {
	return truncate(clean(value), liveFieldLimit)
}

// lastLine берёт последнюю непустую строку вывода: перед отчётом канала
// docker compose успевает напечатать своё.
func lastLine(output []byte) string {
	lines := strings.Split(string(output), "\n")
	for index := len(lines) - 1; index >= 0; index-- {
		if text := strings.TrimSpace(lines[index]); text != "" {
			return text
		}
	}
	return ""
}

// parseLive разбирает отчёт канала. Разбор строгий только там, где от поля
// зависит смысл: версия контракта и состояние обязательны, остальное
// необязательно и расширяемо.
func parseLive(output []byte) (*Live, error) {
	line := lastLine(output)
	if line == "" {
		return nil, errors.New("канал сессий не ответил отчётом")
	}
	var stored struct {
		API      int    `json:"api"`
		State    string `json:"state"`
		Hint     string `json:"hint"`
		Detail   string `json:"detail"`
		Console  string `json:"console"`
		Skipped  int    `json:"skipped"`
		Sessions []struct {
			Name     string `json:"name"`
			Created  int64  `json:"created"`
			Attached bool   `json:"attached"`
			Command  string `json:"command"`
		} `json:"sessions"`
	}
	if err := json.Unmarshal([]byte(line), &stored); err != nil {
		return nil, errors.New("отчёт канала сессий не является строкой JSON")
	}
	if stored.API != liveAPI {
		return nil, errors.New("отчёт канала сессий не соответствует версии контракта " +
			strconv.Itoa(liveAPI))
	}
	if strings.TrimSpace(stored.State) == "" {
		return nil, errors.New("в отчёте канала сессий нет состояния")
	}
	report := &Live{State: liveField(stored.State), Hint: liveField(stored.Hint),
		Detail: liveField(stored.Detail), Console: liveField(stored.Console)}
	if stored.Skipped > 0 {
		report.Skipped = stored.Skipped
	}
	for index, entry := range stored.Sessions {
		if index >= liveSessionLimit {
			break
		}
		name := liveField(entry.Name)
		if name == "" {
			continue
		}
		created := entry.Created
		if created < 0 {
			created = 0
		}
		report.Sessions = append(report.Sessions, LiveSession{Name: name, Created: created,
			Attached: entry.Attached, Command: liveField(entry.Command)})
	}
	return report, nil
}

// shellState опрашивает состояние serial-оболочки гостя читающей целью
// vm-shell-check. Отказ опроса не выдумывает ответа: состояние неизвестно, и
// свободной оболочкой оно не подменяется.
func shellState(root, vm string) string {
	output, err := captureManage(root, "vm-shell-check", map[string]string{"VM": vm}, liveTimeout)
	if err != nil {
		return shellUnknown
	}
	var stored struct {
		API   int    `json:"api"`
		Shell string `json:"shell"`
	}
	if err := json.Unmarshal([]byte(lastLine(output)), &stored); err != nil || stored.API != liveAPI {
		return shellUnknown
	}
	switch liveField(stored.Shell) {
	case shellReady:
		return shellReady
	case shellBusy:
		return shellBusy
	default:
		return shellUnknown
	}
}

// directState опрашивает прямой путь в сессии читающей целью
// vm-session-direct-check. Отказ опроса ответа не выдумывает.
func directState(root, vm string) string {
	output, err := captureManage(root, "vm-session-direct-check",
		map[string]string{"VM": vm}, liveTimeout)
	if err != nil {
		return directUnknown
	}
	var stored struct {
		API    int    `json:"api"`
		Direct string `json:"direct"`
	}
	if err := json.Unmarshal([]byte(lastLine(output)), &stored); err != nil || stored.API != liveAPI {
		return directUnknown
	}
	switch liveField(stored.Direct) {
	case directReady:
		return directReady
	case directSilent:
		return directSilent
	case directAbsent:
		return directAbsent
	default:
		return directUnknown
	}
}

// liveReport опрашивает канал сессий одного гостя. Отказ опроса не
// смешивается с ответом канала: он отдаётся состоянием no-report, потому что
// про сессии в этом случае неизвестно ничего.
func liveReport(root, vm string) *Live {
	output, err := captureManage(root, "vm-sessions-check",
		map[string]string{"VM": vm}, liveTimeout)
	report, parseErr := parseLive(output)
	if parseErr == nil {
		report.Shell = shellState(root, vm)
		report.Direct = directState(root, vm)
		return report
	}
	trouble := parseErr
	if err != nil {
		// Цель объяснила отказ сама: её сообщение точнее нашего разбора.
		trouble = err
	}
	return &Live{State: liveNoReport, Hint: "Канал сессий не опрошен: о живых сессиях ничего не известно.",
		Detail: liveField(trouble.Error()), Shell: shellState(root, vm),
		Direct: directState(root, vm)}
}

// liveCache помнит отчёты канала: опрос идёт не чаще liveTTL и только для
// запущенного гостя. Ключ собирается из корня и имени: один и тот же гость в
// разных каталогах проекта — разные гости.
type liveCache struct {
	mutex   sync.Mutex
	reports map[string]liveEntry
}

type liveEntry struct {
	at     time.Time
	report *Live
}

// channelCache — общий кэш отчётов канала для всех режимов CLI.
var channelCache = &liveCache{}

func liveKey(root, vm string) string {
	return root + "\x00" + vm
}

// lookup отдаёт сохранённый отчёт, если он ещё не устарел.
func (cache *liveCache) lookup(root, vm string, now time.Time) (*Live, bool) {
	cache.mutex.Lock()
	defer cache.mutex.Unlock()
	entry, known := cache.reports[liveKey(root, vm)]
	if !known || now.Sub(entry.at) >= liveTTL || now.Before(entry.at) {
		return nil, false
	}
	return entry.report, true
}

// remember сохраняет отчёт вместе со временем опроса.
func (cache *liveCache) remember(root, vm string, report *Live, now time.Time) {
	cache.mutex.Lock()
	defer cache.mutex.Unlock()
	if cache.reports == nil {
		cache.reports = make(map[string]liveEntry)
	}
	cache.reports[liveKey(root, vm)] = liveEntry{at: now, report: report}
}

// clear забывает всё сохранённое: так клавиша обновления спрашивает канал
// заново, не дожидаясь срока.
func (cache *liveCache) clear() {
	cache.mutex.Lock()
	defer cache.mutex.Unlock()
	cache.reports = nil
}

// applyLive заполняет живые сессии гостей. Опрашиваются только запущенные:
// у остановленного живых сессий нет по определению, и состояние контейнеров
// хост знает сам. Гости опрашиваются одновременно, иначе список ждал бы
// каждого по очереди.
func applyLive(root string, vms []VM, cache *liveCache, now time.Time) {
	var wait sync.WaitGroup
	for index := range vms {
		if !vms[index].Running() {
			continue
		}
		if report, known := cache.lookup(root, vms[index].Name, now); known {
			vms[index].Live = report
			continue
		}
		wait.Add(1)
		go func(index int) {
			defer wait.Done()
			report := liveReport(root, vms[index].Name)
			cache.remember(root, vms[index].Name, report, time.Now())
			vms[index].Live = report
		}(index)
	}
	wait.Wait()
}

// liveText — колонка живых сессий. Данные только из канала: старый образ,
// пустой список и отказ канала различаются текстом, и ни одно состояние не
// выглядит как другое.
func liveText(vm VM) string {
	if vm.Live == nil {
		switch {
		case !vm.StatusKnown:
			return colors.muted("неизвестно")
		case !vm.Running():
			return colors.muted("не запущена")
		default:
			return colors.muted("не опрошен")
		}
	}
	text := ""
	switch {
	case vm.Live.State == liveLive && len(vm.Live.Sessions) > 0:
		text = colors.good(sessionCount(len(vm.Live.Sessions))) + ": " + liveNames(vm.Live.Sessions)
	case vm.Live.State == liveLive:
		// Канал ответил списком, но список пуст: это тот же «сессий нет».
		text = colors.muted(liveTexts[liveEmpty])
	default:
		known, named := liveTexts[vm.Live.State]
		if !named {
			// Состояние расширяемо в рамках api = 1: незнакомое показывается
			// как есть, а не превращается в «сессий нет».
			known = vm.Live.State
		}
		if liveFailures[vm.Live.State] || !named {
			text = colors.bad(known)
		} else {
			text = colors.muted(known)
		}
	}
	if vm.Live.Skipped > 0 {
		text += colors.bad(", пропущено " + strconv.Itoa(vm.Live.Skipped))
	}
	if vm.Live.consoleBusy() {
		text += colors.muted(", консоль занята")
	}
	if !vm.Live.liveWorks() || len(vm.Live.Sessions) == 0 {
		// Пустой список не означает, что в госте ничего не работает: агент,
		// запущенный прямо в serial-оболочке, сессией tmux не является и
		// каналу не виден вовсе. Отметка оболочки отвечает на это отдельно.
		if vm.Live.shellWorking() {
			text += colors.muted(", на serial работает приложение")
		}
	}
	return text
}

// liveNames перечисляет живые сессии командами: имя сессии в колонку не
// помещается, а команда отвечает на вопрос «что там работает». Подключённая
// сессия помечается звёздочкой.
func liveNames(sessions []LiveSession) string {
	shown := make([]string, 0, liveShown+1)
	for _, session := range sessions {
		if len(shown) == liveShown {
			shown = append(shown, "+"+strconv.Itoa(len(sessions)-liveShown))
			break
		}
		name := session.Command
		if name == "" {
			name = session.Name
		}
		if session.Attached {
			name += "*"
		}
		shown = append(shown, name)
	}
	return strings.Join(shown, ", ")
}

// sessionCount склоняет число сессий по-русски.
func sessionCount(count int) string {
	number := strconv.Itoa(count)
	if tail := count % 100; tail >= 11 && tail <= 14 {
		return number + " сессий"
	}
	switch count % 10 {
	case 1:
		return number + " сессия"
	case 2, 3, 4:
		return number + " сессии"
	}
	return number + " сессий"
}
