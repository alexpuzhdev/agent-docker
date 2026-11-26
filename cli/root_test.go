package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// writeProject создаёт каталог с маркером корня проекта.
func writeProject(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "v2"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, marker), []byte("#!/bin/bash\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	return root
}

func TestFindRootExplicit(t *testing.T) {
	root := writeProject(t)
	found, err := findRoot(root)
	if err != nil {
		t.Fatal(err)
	}
	if resolved, _ := filepath.EvalSymlinks(found); resolved != mustResolve(t, root) {
		t.Fatalf("найден %q, ожидался %q", found, root)
	}
	if _, err := findRoot(t.TempDir()); err == nil {
		t.Fatal("каталог без маркера не должен приниматься")
	}
}

func TestFindRootFromEnvironment(t *testing.T) {
	root := writeProject(t)
	t.Setenv("AGENT_DOCKER_ROOT", root)
	found, err := findRoot("")
	if err != nil {
		t.Fatal(err)
	}
	if mustResolve(t, found) != mustResolve(t, root) {
		t.Fatalf("найден %q, ожидался %q", found, root)
	}
	t.Setenv("AGENT_DOCKER_ROOT", t.TempDir())
	if _, err := findRoot(""); err == nil {
		t.Fatal("неверный AGENT_DOCKER_ROOT должен давать ошибку")
	}
}

// chdir переводит тест в чужой каталог и возвращает прежний после него:
// иначе поиск вверх от рабочего каталога нашёл бы сам проект agent-docker,
// внутри которого идут тесты.
func chdir(t *testing.T, directory string) {
	t.Helper()
	previous, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chdir(directory); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := os.Chdir(previous); err != nil {
			t.Fatal(err)
		}
	})
}

// bindRoot записывает привязку каталога проекта так же, как cli/install.sh.
func bindRoot(t *testing.T, root string) {
	t.Helper()
	settings := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", settings)
	path := filepath.Join(settings, rootConfig)
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(root+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
}

// Установленный бинарник лежит вне проекта, поэтому запуск из чужого каталога
// опирается на привязку: без неё корень найти неоткуда.
func TestFindRootFromForeignDirectory(t *testing.T) {
	root := writeProject(t)
	foreign := t.TempDir()
	chdir(t, foreign)
	t.Setenv("AGENT_DOCKER_ROOT", "")

	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	if _, err := findRoot(""); err == nil {
		t.Fatal("без привязки из чужого каталога корень находиться не должен")
	}

	bindRoot(t, root)
	found, err := findRoot("")
	if err != nil {
		t.Fatal(err)
	}
	if mustResolve(t, found) != mustResolve(t, root) {
		t.Fatalf("найден %q, ожидался %q", found, root)
	}
}

// Привязка стоит последней: явный путь и переменная окружения её перебивают,
// а переехавший каталог даёт понятный отказ вместо общего «не найден».
func TestFindRootPrefersExplicitOverBinding(t *testing.T) {
	bound := writeProject(t)
	asked := writeProject(t)
	foreign := t.TempDir()
	chdir(t, foreign)
	bindRoot(t, bound)

	t.Setenv("AGENT_DOCKER_ROOT", "")
	found, err := findRoot(asked)
	if err != nil {
		t.Fatal(err)
	}
	if mustResolve(t, found) != mustResolve(t, asked) {
		t.Fatalf("явный путь должен быть главнее привязки, найден %q", found)
	}

	t.Setenv("AGENT_DOCKER_ROOT", asked)
	found, err = findRoot("")
	if err != nil {
		t.Fatal(err)
	}
	if mustResolve(t, found) != mustResolve(t, asked) {
		t.Fatalf("AGENT_DOCKER_ROOT должен быть главнее привязки, найден %q", found)
	}

	t.Setenv("AGENT_DOCKER_ROOT", "")
	bindRoot(t, filepath.Join(foreign, "уехал"))
	_, err = findRoot("")
	if err == nil {
		t.Fatal("переехавшая привязка должна давать отказ")
	}
	if !strings.Contains(err.Error(), rootConfig) {
		t.Fatalf("отказ должен называть файл привязки: %v", err)
	}
}

func TestWalkUpFindsParent(t *testing.T) {
	root := writeProject(t)
	nested := filepath.Join(root, "v2", "tests")
	if err := os.MkdirAll(nested, 0o700); err != nil {
		t.Fatal(err)
	}
	found, ok := walkUp(nested)
	if !ok || mustResolve(t, found) != mustResolve(t, root) {
		t.Fatalf("получено %q %v", found, ok)
	}
	if _, ok := walkUp(t.TempDir()); ok {
		t.Fatal("каталог без маркера не должен находиться")
	}
}

func TestStateRootAndPrefix(t *testing.T) {
	t.Setenv("V2_STATE_DIR", "")
	if got := stateRoot("/opt/agent-docker"); got != filepath.Join("/opt/agent-docker", defaultStateDir) {
		t.Fatalf("получено %q", got)
	}
	t.Setenv("V2_STATE_DIR", "/var/lib/agent-docker")
	if got := stateRoot("/opt/agent-docker"); got != "/var/lib/agent-docker" {
		t.Fatalf("получено %q", got)
	}
	// Относительное значение manage.sh считает от корня проекта.
	t.Setenv("V2_STATE_DIR", "state")
	if got := stateRoot("/opt/agent-docker"); got != filepath.Join("/opt/agent-docker", "state") {
		t.Fatalf("получено %q", got)
	}
	t.Setenv("V2_PROJECT_PREFIX", "")
	if got := projectPrefix(); got != defaultProjectPrefix {
		t.Fatalf("получено %q", got)
	}
	t.Setenv("V2_PROJECT_PREFIX", "stand")
	if got := projectPrefix(); got != "stand" {
		t.Fatalf("получено %q", got)
	}
}

func mustResolve(t *testing.T, path string) string {
	t.Helper()
	resolved, err := filepath.EvalSymlinks(path)
	if err != nil {
		t.Fatal(err)
	}
	return resolved
}
