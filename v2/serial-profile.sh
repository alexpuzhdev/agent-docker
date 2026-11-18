case "$-" in
    *i*)
        if [ -n "${BASH_VERSION:-}" ] && [ "$(id -un)" = agent ] && [ "$(tty 2>/dev/null)" = /dev/ttyS0 ]; then
            agent_docker_original_prompt_command=${PROMPT_COMMAND-}
            agent_docker_shell_ready() {
                PROMPT_COMMAND=$agent_docker_original_prompt_command
                printf '\033]9999;agent-docker-shell-ready\007'
            }
            PROMPT_COMMAND="${PROMPT_COMMAND:+$PROMPT_COMMAND; }agent_docker_shell_ready"
        fi
        ;;
esac
