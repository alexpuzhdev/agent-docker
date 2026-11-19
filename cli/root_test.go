package main

import (
	"os"
	"path/filepath"
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
