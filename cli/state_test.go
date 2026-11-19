package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fakeToken — заведомо фиктивный токен, присутствие которого в выводе
// означало бы утечку.
const fakeToken = "ghp-fake-token-0123456789"

// writeVM раскладывает состояние гостя так же, как это делает manage.sh.
func writeVM(t *testing.T, state, name, resources, git string) {
	t.Helper()
	dir := filepath.Join(state, name)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	if resources != "" {
		if err := os.WriteFile(filepath.Join(dir, "resources"), []byte(resources), 0o600); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, "upstream"), []byte("http://proxy.invalid:3128\n"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if git != "" {
		if err := os.MkdirAll(filepath.Join(dir, "git"), 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, "git", "repository.json"), []byte(git), 0o600); err != nil {
			t.Fatal(err)
		}
	}
}

func githubConfig(write bool, branch string) string {
	mode := "false"
	if write {
		mode = "true"
	}
	return `{"provider":"github","origin":"https://github.com","repository":"owner/name","token":"` +
		fakeToken + `","write":` + mode + `,"writeBranch":"` + branch + `"}`
}

func TestLoadVMsReadsState(t *testing.T) {
	state := t.TempDir()
	writeVM(t, state, "vm-02", "4 8192 40\n", githubConfig(true, "work"))
	writeVM(t, state, "vm-01", "2 4096 12\n", "")
	writeVM(t, state, "vm-10", "", "")
	writeVM(t, state, "не-vm", "2 4096 12\n", "")
	if err := os.WriteFile(filepath.Join(state, "файл"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}

	vms, err := loadVMs(state)
	if err != nil {
		t.Fatal(err)
	}
	names := make([]string, 0, len(vms))
	for _, vm := range vms {
		names = append(names, vm.Name)
	}
	if strings.Join(names, ",") != "vm-01,vm-02,vm-10" {
		t.Fatalf("получен список %v", names)
	}
	if vms[1].Resources == nil || vms[1].Resources.VCPU != 4 || vms[1].Resources.RAMMB != 8192 ||
		vms[1].Resources.DiskGB != 40 {
		t.Fatalf("ресурсы разобраны неверно: %+v", vms[1].Resources)
	}
	if !vms[1].Proxy || !vms[1].Configured() {
		t.Fatal("vm-02 должна считаться настроенной")
	}
	if vms[2].Configured() {
		t.Fatal("vm-10 без ресурсов не настроена")
	}
	if vms[1].Git == nil || vms[1].Git.Repository != "owner/name" || !vms[1].Git.Write ||
		vms[1].Git.Branch != "work" || vms[1].Git.Provider != "github" {
		t.Fatalf("назначение Git разобрано неверно: %+v", vms[1].Git)
	}
	if vms[0].Git != nil {
		t.Fatal("у vm-01 назначения Git нет")
	}
}

func TestLoadVMsMissingStateDirectory(t *testing.T) {
	vms, err := loadVMs(filepath.Join(t.TempDir(), "нет"))
	if err != nil || vms != nil {
		t.Fatalf("получено %v %v", vms, err)
	}
}

func TestReadGitHidesToken(t *testing.T) {
	state := t.TempDir()
	writeVM(t, state, "vm-01", "2 4096 12\n", githubConfig(true, "work"))
	vms, err := loadVMs(state)
	if err != nil {
		t.Fatal(err)
	}
	applyStatuses(vms, map[string]map[string]string{"vm-01": {"vm": "Up 2 hours"}})
	for _, text := range []string{renderList(vms), renderStatus(vms[0]), gitText(vms[0])} {
		if strings.Contains(text, fakeToken) {
			t.Fatalf("токен попал в вывод: %q", text)
		}
	}
	// Токена не должно быть ни в одном поле разобранного назначения.
	if strings.Contains(formatGit(vms[0].Git), fakeToken) {
		t.Fatal("токен попал в разобранное назначение Git")
	}
}

// formatGit склеивает все поля назначения, чтобы тест проверил их содержимое.
func formatGit(git *Git) string {
	if git == nil {
		return ""
	}
	return git.Provider + " " + git.Origin + " " + git.Repository + " " + git.Branch
}

func TestReadGitGitLabUsesProjectField(t *testing.T) {
	state := t.TempDir()
	writeVM(t, state, "vm-01", "2 4096 12\n",
		`{"provider":"gitlab","origin":"https://gitlab.example.com","project":"group/sub/app","token":"`+
			fakeToken+`","write":false,"writeBranch":""}`)
	vms, err := loadVMs(state)
	if err != nil {
		t.Fatal(err)
	}
	if vms[0].Git == nil || vms[0].Git.Repository != "group/sub/app" ||
		vms[0].Git.Origin != "https://gitlab.example.com" {
		t.Fatalf("проект GitLab разобран неверно: %+v", vms[0].Git)
	}
	if text := gitText(vms[0]); !strings.HasPrefix(text, "gitlab group/sub/app") {
		t.Fatalf("краткое описание GitLab: %q", text)
	}
}

func TestReadGitBrokenConfiguration(t *testing.T) {
	cases := map[string]string{
		"не JSON":            "не json",
		"нет репозитория":    `{"provider":"github","origin":"https://github.com","write":false}`,
		"пустой объект":      `{}`,
		"репозиторий числом": `{"provider":"github","repository":123}`,
	}
	for name, content := range cases {
		t.Run(name, func(t *testing.T) {
			state := t.TempDir()
			writeVM(t, state, "vm-01", "2 4096 12\n", content)
			vms, err := loadVMs(state)
			if err != nil {
				t.Fatal(err)
			}
			if !vms[0].GitBroken || vms[0].Git != nil {
				t.Fatalf("повреждённая настройка должна помечаться: %+v", vms[0])
			}
			if gitText(vms[0]) != "повреждена" {
				t.Fatalf("описание повреждённой настройки: %q", gitText(vms[0]))
			}
		})
	}
}

func TestReadGitWriteWithoutBranch(t *testing.T) {
	state := t.TempDir()
	writeVM(t, state, "vm-01", "2 4096 12\n", githubConfig(false, "work"))
	vms, err := loadVMs(state)
	if err != nil {
		t.Fatal(err)
	}
	if vms[0].Git.Branch != "" {
		t.Fatal("без разрешения записи ветка не показывается")
	}
	if strings.Contains(gitText(vms[0]), "rw:") {
		t.Fatalf("режим чтения не должен выглядеть как запись: %q", gitText(vms[0]))
	}
}

func TestReadResourcesRejectsGarbage(t *testing.T) {
	for _, content := range []string{"2 4096", "2 4096 12 7", "два 4096 12", "0 4096 12", "-1 4096 12", ""} {
		state := t.TempDir()
		writeVM(t, state, "vm-01", content+"\n", "")
		vms, err := loadVMs(state)
		if err != nil {
			t.Fatal(err)
		}
		if vms[0].Resources != nil {
			t.Fatalf("строка %q не должна разбираться как ресурсы", content)
		}
	}
}

func TestValidName(t *testing.T) {
	valid := []string{"vm-01", "vm-02", "vm-254", "vm-99"}
	invalid := []string{"vm-0", "vm-00", "vm-255", "vm-300", "vm-1a", "VM-01", "vm01", "vm-01 ", "../vm-01"}
	for _, name := range valid {
		if !validName(name) {
			t.Fatalf("%q должно приниматься", name)
		}
	}
	for _, name := range invalid {
		if validName(name) {
			t.Fatalf("%q должно отклоняться", name)
		}
	}
}
