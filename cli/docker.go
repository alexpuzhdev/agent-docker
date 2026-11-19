package main

import (
	"errors"
	"os/exec"
	"strings"
)

// statusFormat просит docker отдать проект, сервис и статус каждого контейнера.
const statusFormat = `{{.Label "com.docker.compose.project"}}	{{.Label "com.docker.compose.service"}}	{{.Status}}`

// statuses возвращает состояние контейнеров по имени VM и сервису.
// Один вызов docker обслуживает весь список, поэтому ls не замедляется с
// ростом числа гостей.
func statuses(prefix string) (map[string]map[string]string, error) {
	output, err := exec.Command("docker", "ps", "--all", "--no-trunc", "--format", statusFormat).Output()
	if err != nil {
		var exit *exec.ExitError
		if errors.As(err, &exit) {
			return nil, errors.New("docker не ответил на запрос состояния контейнеров")
		}
		return nil, errors.New("docker недоступен: " + err.Error())
	}
	found := make(map[string]map[string]string)
	for _, line := range strings.Split(string(output), "\n") {
		fields := strings.Split(line, "\t")
		if len(fields) != 3 || !strings.HasPrefix(fields[0], prefix+"-") {
			continue
		}
		name := strings.TrimPrefix(fields[0], prefix+"-")
		if found[name] == nil {
			found[name] = make(map[string]string)
		}
		// docker ps перечисляет контейнеры от новых к старым: первая запись
		// сервиса самая свежая, более старые перезапуски не затирают её.
		if _, seen := found[name][fields[1]]; !seen {
			found[name][fields[1]] = clean(fields[2])
		}
	}
	return found, nil
}

// applyStatuses раскладывает состояние контейнеров по списку гостей.
func applyStatuses(vms []VM, found map[string]map[string]string) {
	for index := range vms {
		services := found[vms[index].Name]
		vms[index].GuestStatus = services["vm"]
		vms[index].ProxyStatus = services["proxy"]
		vms[index].StatusKnown = true
	}
}
