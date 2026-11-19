package main

import (
	"reflect"
	"strings"
	"testing"
)

func TestParseArgsBuildsManageEnvironment(t *testing.T) {
	cases := []struct {
		name        string
		args        []string
		target      string
		environment map[string]string
	}{
		{
			name:        "запуск одного гостя",
			args:        []string{"up", "vm-02"},
			target:      "vm-up",
			environment: map[string]string{"VM": "vm-02"},
		},
		{
			name:        "запуск всех настроенных гостей",
			args:        []string{"up"},
			target:      "vm-up",
			environment: map[string]string{},
		},
		{
			name:        "создание с бюджетом",
			args:        []string{"new", "--cpu", "4", "--ram", "8192", "--disk", "40", "vm-03"},
			target:      "vm-setup",
			environment: map[string]string{"VM": "vm-03", "VCPU": "4", "RAM_MB": "8192", "DISK_GB": "40"},
		},
		{
			name:   "создание с назначением GitHub",
			args:   []string{"new", "--repo", "owner/name", "--write", "test/branch", "vm-04"},
			target: "vm-setup",
			environment: map[string]string{"VM": "vm-04", "REPO": "owner/name",
				"GIT_WRITE": "1", "GIT_WRITE_BRANCH": "test/branch"},
		},
		{
			name:        "журнал SNI при запуске",
			args:        []string{"up", "--sni-log", "vm-02"},
			target:      "vm-up",
			environment: map[string]string{"VM": "vm-02", "GATEWAY_SNI_LOG": "1"},
		},
		{
			name:   "назначение GitLab",
			args:   []string{"git", "--provider", "gitlab", "--url", "https://gitlab.example.com", "--repo", "group/sub/project", "vm-02"},
			target: "vm-git",
			environment: map[string]string{"VM": "vm-02", "REPO": "group/sub/project",
				"GIT_PROVIDER": "gitlab", "GIT_URL": "https://gitlab.example.com", "GIT_WRITE": "0"},
		},
		{
			name:        "просмотр настройки Git",
			args:        []string{"git", "status", "vm-02"},
			target:      "vm-git-status",
			environment: map[string]string{"VM": "vm-02"},
		},
		{
			name:   "журналы контейнеров",
			args:   []string{"logs", "--service", "proxy", "--tail", "all", "--follow", "vm-02"},
			target: "vm-logs",
			environment: map[string]string{"VM": "vm-02", "LOG_TAIL": "all",
				"LOG_SERVICE": "proxy", "LOG_FOLLOW": "1"},
		},
		{
			name:        "журналы по умолчанию",
			args:        []string{"logs", "vm-02"},
			target:      "vm-logs",
			environment: map[string]string{"VM": "vm-02", "LOG_TAIL": "50", "LOG_SERVICE": ""},
		},
		{
			name:        "агент с возобновлением сессии",
			args:        []string{"claude", "--project", "demo", "--resume", "abc", "vm-02"},
			target:      "claude",
			environment: map[string]string{"VM": "vm-02", "PROJECT": "demo", "RESUME": "abc"},
		},
		{
			name:        "профиль гостя",
			args:        []string{"profile", "vm-02"},
			target:      "vm-profile",
			environment: map[string]string{"VM": "vm-02"},
		},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			act, err := parseArgs(test.args)
			if err != nil {
				t.Fatalf("разбор не удался: %v", err)
			}
			if act.kind != kindManage || act.target != test.target {
				t.Fatalf("получено %s/%s, ожидалось %s/%s", act.kind, act.target, kindManage, test.target)
			}
			if !reflect.DeepEqual(act.environment, test.environment) {
				t.Fatalf("окружение %v, ожидалось %v", act.environment, test.environment)
			}
		})
	}
}

func TestParseArgsLocalCommands(t *testing.T) {
	cases := map[string]struct {
		args []string
		kind string
	}{
		"без аргументов открывается меню": {args: nil, kind: kindMenu},
		"список":                          {args: []string{"ls"}, kind: kindList},
		"состояние":                       {args: []string{"status", "vm-01"}, kind: kindStatus},
		"справка":                         {args: []string{"help"}, kind: kindHelp},
		"версия":                          {args: []string{"version"}, kind: kindVersion},
	}
	for name, test := range cases {
		t.Run(name, func(t *testing.T) {
			act, err := parseArgs(test.args)
			if err != nil {
				t.Fatalf("разбор не удался: %v", err)
			}
			if act.kind != test.kind {
				t.Fatalf("вид %s, ожидался %s", act.kind, test.kind)
			}
		})
	}
}

func TestParseArgsRejects(t *testing.T) {
	cases := map[string][]string{
		"неизвестная команда":          {"destroy", "vm-01"},
		"имя без номера":               {"stop", "vm"},
		"номер вне диапазона":          {"stop", "vm-255"},
		"нулевой номер":                {"stop", "vm-00"},
		"лишний аргумент":              {"stop", "vm-01", "vm-02"},
		"отсутствует имя":              {"stop"},
		"неизвестный провайдер":        {"git", "--provider", "bitbucket", "--repo", "a/b", "vm-01"},
		"GitLab без адреса":            {"git", "--provider", "gitlab", "--repo", "a/b", "vm-01"},
		"адрес без GitLab":             {"git", "--url", "https://example.com", "--repo", "a/b", "vm-01"},
		"ветка без репозитория":        {"git", "--write", "work", "vm-01"},
		"git без репозитория":          {"git", "vm-01"},
		"неизвестный сервис журналов":  {"logs", "--service", "db", "vm-01"},
		"нечисловой tail":              {"logs", "--tail", "много", "vm-01"},
		"отрицательные ресурсы":        {"new", "--cpu", "-1", "vm-01"},
		"resume вне claude":            {"console", "--resume", "abc", "vm-01"},
		"несовместимые флаги удаления": {"rm", "--keep-disk", "--delete-disk", "vm-01"},
		"неизвестный флаг":             {"up", "--turbo", "vm-01"},
	}
	for name, args := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := parseArgs(args); err == nil {
				t.Fatalf("ожидалась ошибка для %v", args)
			}
		})
	}
}

func TestParseRemoveKeepsDiskByDefault(t *testing.T) {
	act, err := parseArgs([]string{"rm", "vm-02"})
	if err != nil {
		t.Fatalf("разбор не удался: %v", err)
	}
	if act.environment["DISK_KEEP"] != "1" {
		t.Fatalf("по умолчанию диск должен сохраняться, получено %q", act.environment["DISK_KEEP"])
	}
	if act.confirm == "" {
		t.Fatal("удаление без --yes должно требовать подтверждения")
	}
	dropped, err := parseArgs([]string{"rm", "--delete-disk", "vm-02"})
	if err != nil {
		t.Fatalf("разбор не удался: %v", err)
	}
	if dropped.environment["DISK_KEEP"] != "0" {
		t.Fatal("--delete-disk должен запрашивать удаление диска")
	}
	if !strings.Contains(dropped.confirm, "диск") {
		t.Fatalf("подтверждение должно называть диск: %q", dropped.confirm)
	}
	silent, err := parseArgs([]string{"rm", "--delete-disk", "--yes", "vm-02"})
	if err != nil {
		t.Fatalf("разбор не удался: %v", err)
	}
	if silent.confirm != "" {
		t.Fatal("--yes отменяет запрос подтверждения")
	}
}

func TestTakeRoot(t *testing.T) {
	rest, root, err := takeRoot([]string{"--root", "/opt/agent-docker", "ls"})
	if err != nil || root != "/opt/agent-docker" || !reflect.DeepEqual(rest, []string{"ls"}) {
		t.Fatalf("получено %v %q %v", rest, root, err)
	}
	rest, root, err = takeRoot([]string{"--root=/opt/agent-docker", "ls"})
	if err != nil || root != "/opt/agent-docker" || !reflect.DeepEqual(rest, []string{"ls"}) {
		t.Fatalf("получено %v %q %v", rest, root, err)
	}
	if _, _, err := takeRoot([]string{"--root"}); err == nil {
		t.Fatal("--root без значения должен быть ошибкой")
	}
	rest, root, err = takeRoot([]string{"ls"})
	if err != nil || root != "" || !reflect.DeepEqual(rest, []string{"ls"}) {
		t.Fatalf("получено %v %q %v", rest, root, err)
	}
}
