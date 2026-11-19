package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"time"
)

// sessionFile хранит последнюю сессию гостя рядом с остальным его состоянием.
const sessionFile = "session"

// sessionLimit ограничивает размер читаемого файла сессии.
const sessionLimit = 8 * 1024

// projectLimit обрезает имя проекта при показе: оно приходит от пользователя.
const projectLimit = 40

// Session — что в этом госте открывали последний раз. Запись ведёт CLI:
// изнутри гостя эти сведения получить нельзя, не заняв его консоль.
type Session struct {
	Agent   string    `json:"agent"`
	Project string    `json:"project"`
	Opened  time.Time `json:"opened"`
}

// agents переводит цель manage.sh в имя агента для записи сессии.
var agents = map[string]string{"claude": "claude", "vm-console": "оболочка"}

// readSession читает последнюю сессию гостя; её отсутствие не ошибка.
func readSession(dir string) *Session {
	file, err := os.Open(filepath.Join(dir, sessionFile))
	if err != nil {
		return nil
	}
	defer file.Close()
	content, err := io.ReadAll(io.LimitReader(file, sessionLimit))
	if err != nil {
		return nil
	}
	var session Session
	if err := json.Unmarshal(content, &session); err != nil || session.Agent == "" {
		return nil
	}
	// Имя проекта задаёт пользователь: управляющие символы из него не должны
	// попадать в терминал и ломать разметку списка.
	session.Agent = truncate(clean(session.Agent), projectLimit)
	session.Project = clean(session.Project)
	return &session
}

// rememberSession записывает открытую сессию. Ошибка записи не должна мешать
// работе с гостем, поэтому возвращается для сообщения, а не для остановки.
func rememberSession(dir, target, project string) error {
	agent, known := agents[target]
	if !known {
		return nil
	}
	// Возврат через консоль не означает смену агента: в ней продолжает
	// работать тот, кого запускали, поэтому прежняя запись сохраняется.
	if previous := readSession(dir); previous != nil && target == "vm-console" {
		agent = previous.Agent
		if project == "" {
			project = previous.Project
		}
	}
	// Каталог не создаётся: запись о сессии не должна порождать гостя,
	// которого никто не настраивал.
	if info, err := os.Stat(dir); err != nil || !info.IsDir() {
		return nil
	}
	content, err := json.Marshal(Session{Agent: clean(agent), Project: clean(project),
		Opened: time.Now().UTC()})
	if err != nil {
		return err
	}
	draft, err := os.CreateTemp(dir, "."+sessionFile+".*")
	if err != nil {
		return err
	}
	defer os.Remove(draft.Name())
	if _, err := draft.Write(append(content, '\n')); err != nil {
		draft.Close()
		return err
	}
	if err := draft.Close(); err != nil {
		return err
	}
	return os.Rename(draft.Name(), filepath.Join(dir, sessionFile))
}

// noteSession запоминает открытую сессию, не прерывая работу при отказе.
func noteSession(root, vm, target, project string, console streams) {
	if vm == "" {
		return
	}
	if err := rememberSession(filepath.Join(stateRoot(root), vm), target, project); err != nil {
		fmt.Fprintln(console.errors, "сессия не записана: "+err.Error())
	}
}

// sessionText описывает сессию для списка: агент, проект и давность.
func sessionText(vm VM, now time.Time) string {
	if vm.Session == nil {
		return "—"
	}
	text := vm.Session.Agent
	if vm.Session.Project != "" {
		text += ": " + truncate(vm.Session.Project, projectLimit)
	}
	if age := humanAge(vm.Session.Opened, now); age != "" {
		text += ", " + age
	}
	return text
}

// humanAge переводит время открытия в короткую русскую давность.
func humanAge(opened, now time.Time) string {
	if opened.IsZero() {
		return ""
	}
	elapsed := now.Sub(opened)
	switch {
	case elapsed < 0:
		return ""
	case elapsed < time.Minute:
		return "только что"
	case elapsed < time.Hour:
		return strconv.Itoa(int(elapsed.Minutes())) + " мин назад"
	case elapsed < 24*time.Hour:
		return strconv.Itoa(int(elapsed.Hours())) + " ч назад"
	default:
		return strconv.Itoa(int(elapsed.Hours()/24)) + " сут назад"
	}
}

// lastSession выбирает цель для возврата в гостя. К уже работающему агенту
// подключаются через консоль: запуск второго агента гость отклонит, потому
// что его оболочка занята. Новый агент запускается, только если сессий не было.
func lastSession(vm VM) string {
	// Остановленный гость поднимается заново и агента в нём нет, сколько бы
	// сессий ни открывали раньше.
	if vm.Session == nil || !vm.Running() {
		return "claude"
	}
	return "vm-console"
}
