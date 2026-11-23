"""A small QR Code encoder (byte mode, versions 1-40) that renders to SVG.

Self-contained so the dashboard needs nothing beyond the Python standard library
and PyYAML. Follows ISO/IEC 18004; the structure follows Project Nayuki's
reference implementation.
"""

# Index 0 is unused; versions are 1-40. Order: L, M, Q, H.
_ECC_CODEWORDS_PER_BLOCK = (
    (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28,
     28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26,
     26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
    (-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30,
     28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28,
     30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
)
_NUM_ERROR_CORRECTION_BLOCKS = (
    (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8,
     8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),
    (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16,
     17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),
    (-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20,
     23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68),
    (-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25,
     25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81),
)
_FORMAT_BITS = (1, 0, 3, 2)  # L, M, Q, H
ECC_LOW, ECC_MEDIUM, ECC_QUARTILE, ECC_HIGH = range(4)


def _bit(value, i):
    return (value >> i) & 1 != 0


def _raw_data_modules(ver):
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        numalign = ver // 7 + 2
        result -= (25 * numalign - 10) * numalign - 55
        if ver >= 7:
            result -= 36
    return result


def _data_codewords(ver, ecl):
    return (_raw_data_modules(ver) // 8
            - _ECC_CODEWORDS_PER_BLOCK[ecl][ver] * _NUM_ERROR_CORRECTION_BLOCKS[ecl][ver])


def _gf_multiply(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_multiply(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_multiply(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_multiply(coef, factor)
    return result


class QrCode:
    def __init__(self, data: bytes, min_ecl=ECC_MEDIUM):
        found = None
        for first in (min_ecl, ECC_LOW):
            for ver in range(1, 41):
                count_bits = 8 if ver <= 9 else 16
                needed = 4 + count_bits + len(data) * 8
                if needed <= _data_codewords(ver, first) * 8:
                    found = ver
                    break
            if found:
                min_ecl = first
                break
        if not found:
            raise ValueError("data too long for a QR code")
        ecl = min_ecl
        for better in range(min_ecl + 1, 4):
            if needed <= _data_codewords(ver, better) * 8:
                ecl = better
        self.version, self.ecl = ver, ecl
        self.size = ver * 4 + 17

        bits = []

        def append(value, length):
            bits.extend((value >> i) & 1 for i in reversed(range(length)))

        append(0b0100, 4)
        append(len(data), count_bits)
        for b in data:
            append(b, 8)
        capacity = _data_codewords(ver, ecl) * 8
        append(0, min(4, capacity - len(bits)))
        append(0, -len(bits) % 8)
        pad = 0xEC
        while len(bits) < capacity:
            append(pad, 8)
            pad ^= 0xEC ^ 0x11
        codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]

        size = self.size
        self.modules = [[False] * size for _ in range(size)]
        self.function = [[False] * size for _ in range(size)]
        self._draw_function_patterns()
        self._draw_codewords(self._add_ecc_and_interleave(codewords))

        best, best_penalty = 0, None
        for mask in range(8):
            self._apply_mask(mask)
            self._draw_format_bits(mask)
            penalty = self._penalty()
            if best_penalty is None or penalty < best_penalty:
                best, best_penalty = mask, penalty
            self._apply_mask(mask)
        self._apply_mask(best)
        self._draw_format_bits(best)
        self.mask = best

    def _set_function(self, x, y, dark):
        self.modules[y][x] = dark
        self.function[y][x] = True

    def _alignment_positions(self):
        ver = self.version
        if ver == 1:
            return []
        numalign = ver // 7 + 2
        step = (ver * 8 + numalign * 3 + 5) // (numalign * 4 - 4) * 2
        result = [self.size - 7 - i * step for i in range(numalign - 1)] + [6]
        return list(reversed(result))

    def _draw_function_patterns(self):
        size = self.size
        for i in range(size):
            self._set_function(6, i, i % 2 == 0)
            self._set_function(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        self._set_function(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        positions = self._alignment_positions()
        last = len(positions) - 1
        for i, px in enumerate(positions):
            for j, py in enumerate(positions):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set_function(px + dx, py + dy, max(abs(dx), abs(dy)) != 1)
        self._draw_format_bits(0)
        if self.version >= 7:
            rem = self.version
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.version << 12 | rem
            for i in range(18):
                a, b = size - 11 + i % 3, i // 3
                self._set_function(a, b, _bit(bits, i))
                self._set_function(b, a, _bit(bits, i))

    def _draw_format_bits(self, mask):
        data = _FORMAT_BITS[self.ecl] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        size = self.size
        for i in range(0, 6):
            self._set_function(8, i, _bit(bits, i))
        self._set_function(8, 7, _bit(bits, 6))
        self._set_function(8, 8, _bit(bits, 7))
        self._set_function(7, 8, _bit(bits, 8))
        for i in range(9, 15):
            self._set_function(14 - i, 8, _bit(bits, i))
        for i in range(0, 8):
            self._set_function(size - 1 - i, 8, _bit(bits, i))
        for i in range(8, 15):
            self._set_function(8, size - 15 + i, _bit(bits, i))
        self._set_function(8, size - 8, True)

    def _add_ecc_and_interleave(self, data):
        ver, ecl = self.version, self.ecl
        numblocks = _NUM_ERROR_CORRECTION_BLOCKS[ecl][ver]
        blockecclen = _ECC_CODEWORDS_PER_BLOCK[ecl][ver]
        rawcodewords = _raw_data_modules(ver) // 8
        numshort = numblocks - rawcodewords % numblocks
        shortlen = rawcodewords // numblocks
        divisor = _rs_divisor(blockecclen)
        blocks, k = [], 0
        for i in range(numblocks):
            dat = data[k:k + shortlen - blockecclen + (0 if i < numshort else 1)]
            k += len(dat)
            ecc = _rs_remainder(dat, divisor)
            if i < numshort:
                dat.append(0)
            blocks.append(dat + ecc)
        result = []
        for i in range(len(blocks[0])):
            for j, blk in enumerate(blocks):
                if i != shortlen - blockecclen or j >= numshort:
                    result.append(blk[i])
        return result

    def _draw_codewords(self, data):
        size = self.size
        i, total = 0, len(data) * 8
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            upward = (right + 1) & 2 == 0
            for vert in range(size):
                y = size - 1 - vert if upward else vert
                for j in range(2):
                    x = right - j
                    if not self.function[y][x] and i < total:
                        self.modules[y][x] = _bit(data[i >> 3], 7 - (i & 7))
                        i += 1
            right -= 2

    _MASKS = (
        lambda x, y: (x + y) % 2 == 0,
        lambda x, y: y % 2 == 0,
        lambda x, y: x % 3 == 0,
        lambda x, y: (x + y) % 3 == 0,
        lambda x, y: (x // 3 + y // 2) % 2 == 0,
        lambda x, y: x * y % 2 + x * y % 3 == 0,
        lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
        lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
    )

    def _apply_mask(self, mask):
        fn = self._MASKS[mask]
        for y in range(self.size):
            row, func = self.modules[y], self.function[y]
            for x in range(self.size):
                if not func[x] and fn(x, y):
                    row[x] = not row[x]

    def _penalty(self):
        size, m = self.size, self.modules
        penalty = 0
        lines = [m[y] for y in range(size)] + [[m[y][x] for y in range(size)] for x in range(size)]
        finder_a = [True, False, True, True, True, False, True, False, False, False, False]
        finder_b = finder_a[::-1]
        for line in lines:
            run, prev = 0, None
            for v in line:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        penalty += run - 2
                    run, prev = 1, v
            if run >= 5:
                penalty += run - 2
            padded = [False] * 4 + list(line) + [False] * 4
            for i in range(len(padded) - 10):
                window = padded[i:i + 11]
                if window == finder_a or window == finder_b:
                    penalty += 40
        for y in range(size - 1):
            for x in range(size - 1):
                c = m[y][x]
                if c == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    penalty += 3
        dark = sum(sum(row) for row in m)
        total = size * size
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        penalty += max(k, 0) * 10
        return penalty

    def to_svg(self, border=4):
        dim = self.size + border * 2
        parts = []
        for y in range(self.size):
            for x in range(self.size):
                if self.modules[y][x]:
                    parts.append(f"M{x + border},{y + border}h1v1h-1z")
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dim} {dim}" '
                f'shape-rendering="crispEdges" role="img" aria-label="QR code">'
                f'<rect width="100%" height="100%" fill="#fff"/>'
                f'<path d="{"".join(parts)}" fill="#000"/></svg>')

    def to_pbm(self, border=4, scale=4):
        dim = (self.size + border * 2) * scale
        rows = []
        for y in range(-border, self.size + border):
            row = []
            for x in range(-border, self.size + border):
                dark = 0 <= x < self.size and 0 <= y < self.size and self.modules[y][x]
                row.extend(["1" if dark else "0"] * scale)
            rows.extend([" ".join(row)] * scale)
        return f"P1\n{dim} {dim}\n" + "\n".join(rows) + "\n"


def svg(text: str) -> str:
    return QrCode(text.encode("utf-8")).to_svg()
