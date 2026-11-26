package main

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// manageScript выполняет всю работу с Docker, QEMU и шлюзом. CLI готовит
// для него окружение и не повторяет его проверки.
const manageScript = "v2/manage.sh"

// streams — потоки процесса, вынесенные ради тестируемости. Ввод хранится
// одним буфером: иначе строка, прочитанная одним запросом, терялась бы для
// следующего.
type streams struct {
	input  *bufio.Reader
	output io.Writer
	errors io.Writer
}

// managed — переменные, которыми CLI управляет при вызове manage.sh. Те, что
// команда не задаёт, передаются пустыми: экспортированная в оболочке VM или
// VCPU иначе незаметно изменила бы поведение цели.
var managed = []string{"VM", "VCPU", "RAM_MB", "DISK_GB", "REPO", "GIT_PROVIDER", "GIT_URL",
	"GIT_WRITE", "GIT_WRITE_BRANCH", "GIT_READ_BRANCHES", "PROJECT", "RESUME", "LOG_TAIL", "LOG_SERVICE",
	"LOG_FOLLOW", "DISK_KEEP", "GATEWAY_SNI_LOG"}

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

// captureManage выполняет читающую цель manage.sh и возвращает её вывод.
// Терминал цели не достаётся: опрос канала идёт, пока список держит ввод в
// неформатированном режиме, и дочерний процесс не должен забирать клавиши.
// Отказ приходит с последней строкой stderr: она объясняет причину точнее,
// чем код возврата.
func captureManage(root, target string, environment map[string]string,
	limit time.Duration) ([]byte, error) {
	deadline, cancel := context.WithTimeout(context.Background(), limit)
	defer cancel()
	command := exec.CommandContext(deadline, "bash", filepath.Join(root, manageScript), target)
	command.Dir = root
	command.Env = append(os.Environ(), flatten(environment)...)
	command.Stdin = nil
	var output, failure bytes.Buffer
	command.Stdout = &output
	command.Stderr = &failure
	err := command.Run()
	data := output.Bytes()
	if len(data) > liveReplyLimit {
		data = data[:liveReplyLimit]
	}
	if err == nil {
		return data, nil
	}
	if deadline.Err() != nil {
		return data, errors.New(target + ": цель не ответила в отведённый срок")
	}
	if reason := lastLine(failure.Bytes()); reason != "" {
		return data, errors.New(reason)
	}
	return data, err
}

// flatten раскладывает переменные в отсортированный список KEY=VALUE,
// добавляя пустые значения для управляемых переменных, которых нет в наборе.
func flatten(environment map[string]string) []string {
	values := make(map[string]string, len(managed)+len(environment))
	for _, name := range managed {
		values[name] = ""
	}
	for key, value := range environment {
		values[key] = value
	}
	pairs := make([]string, 0, len(values))
	for key, value := range values {
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
	answer, err := console.input.ReadString('\n')
	if err != nil && answer == "" {
		return false
	}
	return strings.TrimSpace(answer) == vm
}
