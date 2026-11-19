package main

import (
	"strconv"
	"strings"
	"unicode/utf8"
)

// listHeaders — заголовки таблицы agd ls.
var listHeaders = []string{"VM", "СОСТОЯНИЕ", "CPU/RAM/ДИСК", "ПРОКСИ", "GIT"}

// statusText описывает состояние гостя одним словом. Строка docker ps не
// переводится дословно: её полный текст показывает agd status.
func statusText(vm VM) string {
	switch {
	case vm.Running():
		return "работает"
	case vm.GuestStatus != "":
		return "остановлена"
	case !vm.StatusKnown:
		return "неизвестно"
	case !vm.Configured():
		return "не настроена"
	default:
		return "не запускалась"
	}
}

// resourcesText печатает бюджет гостя как cpu/ram/диск.
func resourcesText(vm VM) string {
	if vm.Resources == nil {
		return "—"
	}
	return strconv.Itoa(vm.Resources.VCPU) + "/" +
		strconv.Itoa(vm.Resources.RAMMB) + "/" +
		strconv.Itoa(vm.Resources.DiskGB)
}

// gitText кратко описывает назначение Git-шлюза. Токен не читается и не
// печатается: readGit его не разбирает.
func gitText(vm VM) string {
	if vm.GitBroken {
		return "повреждена"
	}
	if vm.Git == nil {
		return "—"
	}
	summary := vm.Git.Provider + " " + vm.Git.Repository
	if vm.Git.Write && vm.Git.Branch != "" {
		summary += " rw:" + vm.Git.Branch
	}
	return summary
}

// vmRow собирает строку таблицы для одного гостя.
func vmRow(vm VM) []string {
	proxy := "нет"
	if vm.Proxy {
		proxy = "есть"
	}
	return []string{vm.Name, statusText(vm), resourcesText(vm), proxy, gitText(vm)}
}

// renderList строит таблицу гостей. Пустой список печатает подсказку.
func renderList(vms []VM) string {
	if len(vms) == 0 {
		return "Гостей пока нет. Создайте первого: agd new vm-01\n"
	}
	rows := make([][]string, 0, len(vms))
	for _, vm := range vms {
		rows = append(rows, vmRow(vm))
	}
	return renderTable(listHeaders, rows)
}

// renderTable выравнивает колонки по ширине в символах, а не в байтах,
// чтобы кириллица не ломала разметку.
func renderTable(headers []string, rows [][]string) string {
	widths := make([]int, len(headers))
	for index, header := range headers {
		widths[index] = utf8.RuneCountInString(header)
	}
	for _, row := range rows {
		for index, cell := range row {
			if index < len(widths) && utf8.RuneCountInString(cell) > widths[index] {
				widths[index] = utf8.RuneCountInString(cell)
			}
		}
	}
	var builder strings.Builder
	writeRow(&builder, headers, widths)
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
		padding := widths[index] - utf8.RuneCountInString(cell) + 2
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
	builder.WriteString("Каталог состояния: " + vm.Dir + "\n")
	return builder.String()
}
