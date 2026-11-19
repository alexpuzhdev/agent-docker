package main

import (
	"strconv"
	"strings"
	"time"
)

// listHeaders — заголовки таблицы agd ls.
var listHeaders = []string{"VM", "СОСТОЯНИЕ", "CPU/RAM/ДИСК", "ПРОКСИ", "GIT", "СЕССИЯ"}

// statusText описывает состояние гостя одним словом. Строка docker ps не
// переводится дословно: её полный текст показывает agd status.
func statusText(vm VM) string {
	switch {
	case vm.Running():
		return colors.good("работает")
	case vm.GuestStatus != "":
		return "остановлена"
	case !vm.StatusKnown:
		return colors.muted("неизвестно")
	case !vm.Configured():
		return colors.muted("не настроена")
	default:
		return colors.muted("не запускалась")
	}
}

// resourcesText печатает бюджет гостя как cpu/ram/диск.
func resourcesText(vm VM) string {
	if vm.Resources == nil {
		return colors.muted("—")
	}
	return strconv.Itoa(vm.Resources.VCPU) + "/" +
		strconv.Itoa(vm.Resources.RAMMB) + "/" +
		strconv.Itoa(vm.Resources.DiskGB)
}

// gitText кратко описывает назначение Git-шлюза. Токен не читается и не
// печатается: readGit его не разбирает.
func gitText(vm VM) string {
	if vm.GitBroken {
		return colors.bad("повреждена")
	}
	if vm.Git == nil {
		return colors.muted("—")
	}
	summary := vm.Git.Provider + " " + vm.Git.Repository
	if vm.Git.Write && vm.Git.Branch != "" {
		summary += " rw:" + vm.Git.Branch
	}
	return summary
}

// vmRow собирает строку таблицы для одного гостя.
func vmRow(vm VM, now time.Time) []string {
	proxy := "нет"
	if vm.Proxy {
		proxy = "есть"
	}
	return []string{colors.accent(vm.Name), statusText(vm), resourcesText(vm), proxy,
		gitText(vm), sessionText(vm, now)}
}

// renderList строит таблицу гостей. Пустой список печатает подсказку.
func renderList(vms []VM) string {
	if len(vms) == 0 {
		return "Гостей пока нет. Создайте первого: agd new vm-01\n"
	}
	rows := make([][]string, 0, len(vms))
	now := time.Now()
	for _, vm := range vms {
		rows = append(rows, vmRow(vm, now))
	}
	return renderTable(listHeaders, rows)
}

// renderTable выравнивает колонки по ширине в символах, а не в байтах,
// чтобы кириллица не ломала разметку.
func renderTable(headers []string, rows [][]string) string {
	widths := make([]int, len(headers))
	for index, header := range headers {
		widths[index] = visibleWidth(header)
	}
	for _, row := range rows {
		for index, cell := range row {
			if index < len(widths) && visibleWidth(cell) > widths[index] {
				widths[index] = visibleWidth(cell)
			}
		}
	}
	var header, builder strings.Builder
	writeRow(&header, headers, widths)
	builder.WriteString(colors.muted(strings.TrimSuffix(header.String(), "\n")) + "\n")
	for _, row := range rows {
		writeRow(&builder, row, widths)
	}
	return builder.String()
}

// writeRow печатает одну строку таблицы, не добавляя пробелы после последней
// колонки.
func writeRow(builder *strings.Builder, cells []string, widths []int) {
	for index, cell := range cells {
		builder.WriteString(cell)
		if index == len(cells)-1 {
			break
		}
		padding := widths[index] - visibleWidth(cell) + 2
		builder.WriteString(strings.Repeat(" ", padding))
	}
	builder.WriteString("\n")
}

// renderStatus печатает подробное состояние одного гостя.
func renderStatus(vm VM) string {
	var builder strings.Builder
	builder.WriteString("VM: " + vm.Name + "\n")
	builder.WriteString("Состояние: " + statusText(vm) + "\n")
	if vm.GuestStatus != "" {
		builder.WriteString("Контейнер гостя: " + vm.GuestStatus + "\n")
	}
	if vm.ProxyStatus != "" {
		builder.WriteString("Контейнер шлюза: " + vm.ProxyStatus + "\n")
	}
	if vm.Resources != nil {
		builder.WriteString("Ресурсы: " + strconv.Itoa(vm.Resources.VCPU) + " CPU, " +
			strconv.Itoa(vm.Resources.RAMMB) + " МиБ RAM, " +
			strconv.Itoa(vm.Resources.DiskGB) + " ГиБ диска\n")
	} else {
		builder.WriteString("Ресурсы: не заданы\n")
	}
	if vm.Proxy {
		builder.WriteString("Прокси: настроен\n")
	} else {
		builder.WriteString("Прокси: не настроен\n")
	}
	builder.WriteString("Git: " + gitText(vm) + "\n")
	builder.WriteString("Последняя сессия: " + sessionText(vm, time.Now()) + "\n")
	builder.WriteString("Каталог состояния: " + vm.Dir + "\n")
	return builder.String()
}

// panelHints — подсказка по командам под списком гостей.
var panelHints = []string{
	"/go vm-01 вернуться в сессию   /claude vm-01 запустить агента   /shell vm-01 оболочка",
	"/ls обновить   /up /stop /new /rm   /git /logs /profile   /menu   /help   /quit",
}

// renderPanel рисует список гостей и подсказки над строкой команд. Строки
// разделяются \r\n: терминал находится в неформатированном режиме.
func renderPanel(vms []VM, root string, width int, now time.Time) string {
	if width < 20 {
		width = 20
	}
	lines := []string{truncate(colors.accent("agent-docker "+version), width),
		truncate(colors.muted(root), width), ""}
	if len(vms) == 0 {
		lines = append(lines, truncate("  гостей нет, создайте первого: /new vm-01", width))
	} else {
		rows := make([][]string, 0, len(vms))
		for _, vm := range vms {
			rows = append(rows, vmRow(vm, now))
		}
		for _, row := range strings.Split(strings.TrimRight(renderTable(listHeaders, rows), "\n"), "\n") {
			lines = append(lines, truncate("  "+row, width))
		}
	}
	lines = append(lines, "")
	for _, hint := range panelHints {
		lines = append(lines, truncate(colors.muted(hint), width))
	}
	return strings.Join(lines, "\r\n") + "\r\n"
}
