# Отметка возврата к приглашению для гостевой консоли.
#
# /opt/agent-docker/console.py ждёт её, прежде чем отправить команду в оболочку:
# отправлять вслепую нельзя, иначе байты уйдут работающей на переднем плане
# программе как нажатия клавиш. Отметка печатается при каждом настоящем
# возврате Bash к приглашению, а не один раз за вход.
#
# Последовательность OSC невидима в терминале, в видимом выводе консоли от неё
# ничего не появляется. Хук добавляется последним и возвращает статус
# предыдущей команды, поэтому и пользовательский PROMPT_COMMAND, и PS1 видят
# тот же код завершения, что без него. Повторный вход в профиль хук не дублирует.
case "$-" in
    *i*)
        if [ -n "${BASH_VERSION:-}" ] && [ "$(id -un)" = agent ] && [ "$(tty 2>/dev/null)" = /dev/ttyS0 ]; then
            agent_docker_shell_ready() {
                local agent_docker_status=$?
                printf '\033]9999;agent-docker-shell-ready\007'
                return $agent_docker_status
            }
            case "$(declare -p PROMPT_COMMAND 2>/dev/null)" in
                *agent_docker_shell_ready*)
                    ;;
                'declare -a'*)
                    # PROMPT_COMMAND-массив появился в bash 5.1: там хук идёт
                    # отдельным элементом и выполняется после пользовательских.
                    PROMPT_COMMAND+=(agent_docker_shell_ready)
                    ;;
                *)
                    PROMPT_COMMAND="${PROMPT_COMMAND:+$PROMPT_COMMAND; }agent_docker_shell_ready"
                    ;;
            esac
        fi
        ;;
esac
