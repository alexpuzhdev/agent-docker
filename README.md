# agent-docker v2

На одной Linux-машине Docker Compose запускает отдельный гостевой Debian с KVM для каждого окружения Claude Code. У гостя свой диск и свой сетевой шлюз. Исходники, авторизация и история Claude хранятся на диске гостя.

Нужны GNU Make, Docker с Compose v2, архитектура x86-64, доступ Docker к `/dev/kvm` и действующий HTTP-прокси. При отсутствии базового Docker-образа его загрузка требует настроенного HTTPS-прокси Docker daemon. Подготовленный локальный базовый образ также подходит.

Из корня проекта выполните:

```sh
make
make claude-auth
make claude
```

Первый `make` создаёт `vm-01`, запрашивает прокси, проверяет его, готовит Debian, устанавливает Claude и ждёт подтверждения готовности. Для отдельного окружения:

```sh
make vm-setup VM=vm-02 VCPU=2 RAM_MB=4096 DISK_GB=12
make claude-auth VM=vm-02
make claude VM=vm-02
```

[Подробная инструкция](GUIDE.md)
