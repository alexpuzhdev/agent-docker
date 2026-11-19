package main

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
)

// manageScript выполняет всю работу с Docker, QEMU и шлюзом. CLI готовит
// для него окружение и не повторяет его проверки.
const manageScript = "v2/manage.sh"

// streams — потоки процесса, вынесенные ради тестируемости.
type streams struct {
	input  io.Reader
	output io.Writer
	errors io.Writer
}

// runManage запускает цель manage.sh, наследуя терминал: консоль гостя и
// запросы прокси и токена получают тот же tty.
func runManage(root, target string, environment map[string]string) error {
	command := exec.Command("bash", filepath.Join(root, manageScript), target)
	command.Dir = root
	command.Env = append(os.Environ(), flatten(environment)...)
	command.Stdin = os.Stdin
	command.Stdout = os.Stdout
	command.Stderr = os.Stderr
	return command.Run()
}

// flatten раскладывает переменные в отсортированный список KEY=VALUE.
func flatten(environment map[string]string) []string {
	pairs := make([]string, 0, len(environment))
	for key, value := range environment {
		pairs = append(pairs, key+"="+value)
	}
	sort.Strings(pairs)
	return pairs
}

// exitCode достаёт код возврата дочернего процесса.
func exitCode(err error) int {
	if err == nil {
		return 0
	}
	var exit *exec.ExitError
	if errors.As(err, &exit) && exit.ExitCode() > 0 {
		return exit.ExitCode()
	}
	return 1
}

// confirmed просит подтвердить необратимое действие вводом имени VM.
func confirmed(prompt, vm string, console streams) bool {
	fmt.Fprintln(console.output, prompt)
	fmt.Fprintf(console.output, "Для подтверждения введите имя VM (%s) или пустую строку для отмены: ", vm)
	reader := bufio.NewReader(console.input)
	answer, err := reader.ReadString('\n')
	if err != nil && answer == "" {
		return false
	}
	return strings.TrimSpace(answer) == vm
}
