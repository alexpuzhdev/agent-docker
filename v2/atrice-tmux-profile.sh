# Единый сокет tmux для канала сессий Atrice.
#
# Каталог сокета создан заранее (/usr/lib/tmpfiles.d/atrice-sessions.conf) и
# общий для агентов и гостевой службы. Сессии агентов запускаются именно на
# этом сокете — tmux -S "$ATRICE_TMUX_SOCKET" — иначе служба их не увидит:
# она смотрит ровно один путь и каталогов не создаёт.
ATRICE_TMUX_SOCKET=/run/atrice/sessions/atrice
export ATRICE_TMUX_SOCKET
