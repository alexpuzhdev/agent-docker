package main

import (
	"errors"
	"os"
	"path/filepath"
)

// marker — файл, по которому опознаётся корень проекта agent-docker.
const marker = "v2/manage.sh"

// defaultStateDir — каталог состояния VM относительно корня проекта.
const defaultStateDir = ".v2-state"

// defaultProjectPrefix — префикс проектов Docker Compose.
const defaultProjectPrefix = "agent-docker-v2"

// findRoot ищет корень проекта: явный путь, переменная окружения, каталог
// исполняемого файла, рабочий каталог. В каждом случае просматриваются и
// родительские каталоги, чтобы CLI работал из любого подкаталога.
func findRoot(explicit string) (string, error) {
	if explicit != "" {
		if containsMarker(explicit) {
			return filepath.Abs(explicit)
		}
		return "", errors.New("в указанном каталоге нет " + marker)
	}
	var candidates []string
	if value := os.Getenv("AGENT_DOCKER_ROOT"); value != "" {
		if !containsMarker(value) {
			return "", errors.New("в AGENT_DOCKER_ROOT нет " + marker)
		}
		return filepath.Abs(value)
	}
	if executable, err := os.Executable(); err == nil {
		if resolved, err := filepath.EvalSymlinks(executable); err == nil {
			executable = resolved
		}
		candidates = append(candidates, filepath.Dir(executable))
	}
	if working, err := os.Getwd(); err == nil {
		candidates = append(candidates, working)
	}
	for _, candidate := range candidates {
		if found, ok := walkUp(candidate); ok {
			return found, nil
		}
	}
	return "", errors.New("каталог agent-docker не найден: укажите --root или AGENT_DOCKER_ROOT")
}

// walkUp поднимается от каталога к корню файловой системы в поисках маркера.
func walkUp(start string) (string, bool) {
	current, err := filepath.Abs(start)
	if err != nil {
		return "", false
	}
	for {
		if containsMarker(current) {
			return current, true
		}
		parent := filepath.Dir(current)
		if parent == current {
			return "", false
		}
		current = parent
	}
}

func containsMarker(directory string) bool {
	info, err := os.Stat(filepath.Join(directory, marker))
	return err == nil && info.Mode().IsRegular()
}

// stateRoot возвращает каталог состояния VM с учётом V2_STATE_DIR.
func stateRoot(root string) string {
	if value := os.Getenv("V2_STATE_DIR"); value != "" {
		return value
	}
	return filepath.Join(root, defaultStateDir)
}

// projectPrefix возвращает префикс проектов Compose с учётом V2_PROJECT_PREFIX.
func projectPrefix() string {
	if value := os.Getenv("V2_PROJECT_PREFIX"); value != "" {
		return value
	}
	return defaultProjectPrefix
}
