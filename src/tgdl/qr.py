"""终端二维码。

用半块字符把两行模块压成一行字符，宽度只有原来的 1/2，普通 80 列终端也放得下：

    ▀  上黑下白    ▄  上白下黑    █  全黑    空格  全白
"""

from __future__ import annotations


def render(text: str, *, border: int = 2) -> str:
    import qrcode

    qr = qrcode.QRCode(border=border, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()

    height = len(matrix)
    width = len(matrix[0]) if height else 0
    lines: list[str] = []
    for y in range(0, height, 2):
        row = []
        for x in range(width):
            top = matrix[y][x]
            bottom = matrix[y + 1][x] if y + 1 < height else False
            if top and bottom:
                row.append("█")
            elif top:
                row.append("▀")
            elif bottom:
                row.append("▄")
            else:
                row.append(" ")
        lines.append("".join(row))
    return "\n".join(lines)
