package main

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
)

// marker — файл, по которому опознаётся корень проекта agent-docker.
const marker = "v2/manage.sh"

// defaultStateDir — каталог состояния VM относительно корня проекта.
const defaultStateDir = ".v2-state"

// defaultProjectPrefix — префикс проектов Docker Compose.
const defaultProjectPrefix = "agent-docker-v2"

// rootConfig — файл с путём к настроенному каталогу проекта относительно
// каталога настроек пользователя. Его пишет cli/install.sh по явному
// запросу, CLI только читает: установленный бинарник лежит вне проекта, и
// поиск вверх от него и от текущего каталога ничего не находит.
const rootConfig = "agent-docker/root"

// rootConfigLimit ограничивает читаемый файл привязки: в нём один путь.
const rootConfigLimit = 4096

// rootConfigPath возвращает путь к файлу привязки по XDG: сначала
// XDG_CONFIG_HOME, иначе ~/.config. Пустая строка означает, что каталог
// настроек определить не удалось.
func rootConfigPath() string {
	base := os.Getenv("XDG_CONFIG_HOME")
	if base == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return ""
		}
		base = filepath.Join(home, ".config")
	}
	return filepath.Join(base, rootConfig)
}

// findRoot ищет корень проекта: явный путь, переменная окружения, каталог
// исполняемого файла, рабочий каталог, настроенная привязка. Явный путь и
// переменная должны указывать прямо на корень, а поиск от каталога бинарника
// и от рабочего каталога поднимается вверх, чтобы CLI работал из любого
// подкаталога. Привязка стоит последней намеренно: она нужна установленному
// бинарнику вне проекта и не должна перебивать тот каталог, в котором
// пользователь находится.
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
	if path := rootConfigPath(); path != "" {
		if value, ok := readRootConfig(path); ok {
			if containsMarker(value) {
				return filepath.Abs(value)
			}
			// Привязка есть, но каталог переехал: молчать нельзя, иначе
			// сообщение отправило бы настраивать то, что уже настроено.
			return "", errors.New("в каталоге из " + path + " нет " + marker +
				": поправьте привязку или укажите --root")
		}
	}
	return "", errors.New("каталог agent-docker не найден: укажите --root, " +
		"задайте AGENT_DOCKER_ROOT или привяжите каталог: bash cli/install.sh --bind")
}

// readRootConfig читает путь из файла привязки. Отсутствие файла не ошибка:
// привязки просто нет, и это обычный случай для сборки внутри проекта.
func readRootConfig(path string) (string, bool) {
	content, err := os.ReadFile(path)
	if err != nil || len(content) > rootConfigLimit {
		return "", false
	}
	value := strings.TrimSpace(string(content))
	if value == "" {
		return "", false
	}
	return value, true
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
// Относительное значение считается от корня проекта: manage.sh переходит
// туда до вычисления пути, и CLI должен выбирать тот же каталог.
func stateRoot(root string) string {
	value := os.Getenv("V2_STATE_DIR")
	if value == "" {
		return filepath.Join(root, defaultStateDir)
	}
	if filepath.IsAbs(value) {
		return value
	}
	return filepath.Join(root, value)
}

// projectPrefix возвращает префикс проектов Compose с учётом V2_PROJECT_PREFIX.
func projectPrefix() string {
	if value := os.Getenv("V2_PROJECT_PREFIX"); value != "" {
		return value
	}
	return defaultProjectPrefix
}
