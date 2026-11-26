"""Управляющие последовательности в потоке терминала гостя."""

import re

mouse_modes = frozenset((b'1000', b'1001', b'1002', b'1003', b'1005', b'1006', b'1015', b'1016'))
mouse_enable = re.compile(rb'\x1b\[\?([0-9;]+)h')
TERMINAL_RESET = (b'\x1b[?1000l\x1b[?1001l\x1b[?1002l\x1b[?1003l\x1b[?1005l'
                  b'\x1b[?1006l\x1b[?1015l\x1b[?1016l\x1b[?2004l\x1b[?1049l')


class Stream:
    """Поток гость→хост: по политике мыши снимает её включения.

    Фильтр нужен только для режима mouse=off и работает на границе чтений:
    управляющая последовательность может прийти разорванной на два вызова
    recv, поэтому незакрытый остаток переносится в следующий кадр. Никакие
    другие байты не меняются, и в сторону гостя фильтра нет вовсе.
    """

    def __init__(self, filtered: bool) -> None:
        self.filtered = filtered
        self.carry = b''

    def clean(self, data: bytes) -> bytes:
        if not self.filtered:
            return data
        data = self.carry + data
        self.carry = b''
        # Незавершённый CSI в конце кадра дожидается продолжения. Предел
        # оставляет догонять только осмысленно короткий остаток.
        tail = data.rfind(b'\x1b')
        if tail >= 0 and len(data) - tail <= 128 and re.fullmatch(rb'\x1b(?:\[\??[0-9;]*)?', data[tail:]):
            self.carry = data[tail:]
            data = data[:tail]
        return mouse_enable.sub(self.remove_mouse_modes, data)

    @staticmethod
    def remove_mouse_modes(match: re.Match[bytes]) -> bytes:
        modes = [mode for mode in match[1].split(b';') if mode.lstrip(b'0') not in mouse_modes]
        return b'\x1b[?' + b';'.join(modes) + b'h' if modes else b''
