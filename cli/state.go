package main

import (
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// namePattern — формат имени VM, который принимает manage.sh.
var namePattern = regexp.MustCompile(`^vm-([0-9]{2,3})$`)

// configLimit ограничивает размер читаемого файла настройки Git.
const configLimit = 64 * 1024

// Resources — сохранённый бюджет гостя.
type Resources struct {
	VCPU   int
	RAMMB  int
	DiskGB int
}

// Git — назначение Git-шлюза без токена: поле токена здесь намеренно
// отсутствует, чтобы он не попал ни в вывод, ни в сообщение об ошибке.
type Git struct {
	Provider   string
	Origin     string
	Repository string
	Write      bool
	Branch     string
}

// VM — состояние одного гостя на хосте.
type VM struct {
	Name        string
	Dir         string
	Resources   *Resources
	Proxy       bool
	Git         *Git
	GitBroken   bool
	GuestStatus string
	ProxyStatus string
	StatusKnown bool
	Session     *Session
}

// Configured сообщает, прошла ли VM первоначальную настройку.
func (vm VM) Configured() bool {
	return vm.Resources != nil && vm.Proxy
}

// Running сообщает, запущен ли контейнер гостя.
func (vm VM) Running() bool {
	return strings.HasPrefix(vm.GuestStatus, "Up")
}

// vmNumber возвращает номер гостя из имени или ноль, если имя не подходит.
func vmNumber(name string) int {
	match := namePattern.FindStringSubmatch(name)
	if match == nil {
		return 0
	}
	number, err := strconv.Atoi(match[1])
	if err != nil {
		return 0
	}
	return number
}

// validName проверяет имя VM так же, как set_name в manage.sh.
func validName(name string) bool {
	number := vmNumber(name)
	return number >= 1 && number <= 254
}

// loadVMs читает каталоги состояния и возвращает гостей по возрастанию имени.
func loadVMs(state string) ([]VM, error) {
	entries, err := os.ReadDir(state)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var vms []VM
	for _, entry := range entries {
		if !entry.IsDir() || !validName(entry.Name()) {
			continue
		}
		vms = append(vms, loadVM(filepath.Join(state, entry.Name()), entry.Name()))
	}
	sort.Slice(vms, func(first, second int) bool {
		return vmNumber(vms[first].Name) < vmNumber(vms[second].Name)
	})
	return vms, nil
}

// loadVM собирает состояние одной VM. Отсутствующие файлы не считаются ошибкой:
// частично настроенный гость должен попадать в список со своим признаком.
func loadVM(dir, name string) VM {
	vm := VM{Name: name, Dir: dir}
	if resources, err := readResources(filepath.Join(dir, "resources")); err == nil {
		vm.Resources = resources
	}
	if _, err := os.Stat(filepath.Join(dir, "upstream")); err == nil {
		vm.Proxy = true
	}
	vm.Session = readSession(dir)
	assignment, err := readGit(filepath.Join(dir, "git", "repository.json"))
	switch {
	case err == nil && assignment != nil:
		vm.Git = assignment
	case err != nil && !errors.Is(err, os.ErrNotExist):
		vm.GitBroken = true
	}
	return vm
}

// readResources разбирает строку "vcpu ram disk", которую пишет manage.sh.
func readResources(path string) (*Resources, error) {
	content, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	fields := strings.Fields(string(content))
	if len(fields) != 3 {
		return nil, errors.New("повреждён файл ресурсов")
	}
	values := make([]int, 3)
	for index, field := range fields {
		value, err := strconv.Atoi(field)
		if err != nil || value <= 0 {
			return nil, errors.New("повреждён файл ресурсов")
		}
		values[index] = value
	}
	return &Resources{VCPU: values[0], RAMMB: values[1], DiskGB: values[2]}, nil
}

// readGit читает назначение шлюза. Разбираются только поля отображения:
// токен не попадает в структуру, а ошибки не содержат текста файла.
func readGit(path string) (*Git, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	content, err := io.ReadAll(io.LimitReader(file, configLimit))
	if err != nil {
		return nil, errors.New("не удалось прочитать настройку Git")
	}
	var stored struct {
		Provider    string `json:"provider"`
		Origin      string `json:"origin"`
		Repository  string `json:"repository"`
		Project     string `json:"project"`
		Write       bool   `json:"write"`
		WriteBranch string `json:"writeBranch"`
	}
	if err := json.Unmarshal(content, &stored); err != nil {
		return nil, errors.New("настройка Git повреждена")
	}
	assignment := &Git{
		Provider:   stored.Provider,
		Origin:     stored.Origin,
		Repository: stored.Repository,
		Write:      stored.Write,
		Branch:     stored.WriteBranch,
	}
	if assignment.Provider == "" {
		assignment.Provider = "github"
	}
	if assignment.Provider == "gitlab" {
		assignment.Repository = stored.Project
	}
	if assignment.Repository == "" {
		return nil, errors.New("настройка Git повреждена")
	}
	if !assignment.Write {
		assignment.Branch = ""
	}
	return assignment, nil
}
