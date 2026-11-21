"""Spatial queries and bounded ground navigation for the official arena map."""

import json
import sys
from array import array
from dataclasses import dataclass
from heapq import heappop, heappush
from math import ceil, floor, hypot, sqrt
from pathlib import Path


def inside(x, y, ring):
    result = False
    px, py = ring[-2:]
    for i in range(0, len(ring), 2):
        qx, qy = ring[i : i + 2]
        if (py > y) != (qy > y) and x < px + (qx - px) * (y - py) / (qy - py):
            result = not result
        px, py = qx, qy
    return result


@dataclass(slots=True)
class Prism:
    ring: list
    holes: list
    bottom: float
    top: float
    bounds: tuple

    def contains(self, x, y):
        a, b, c, d = self.bounds
        return (
            a <= x <= b
            and c <= y <= d
            and inside(x, y, self.ring)
            and not any(inside(x, y, hole) for hole in self.holes)
        )


class Arena:
    cell = 6.0
    bucket = 24.0
    neighbors = (
        (-1, 0, 1.0),
        (1, 0, 1.0),
        (0, -1, 1.0),
        (0, 1, 1.0),
        (-1, -1, sqrt(2)),
        (-1, 1, sqrt(2)),
        (1, -1, sqrt(2)),
        (1, 1, sqrt(2)),
    )

    def __init__(self, directory):
        path = Path(directory) / "v2"
        world = json.loads((path / "world.json").read_bytes())
        self.bounds = b = world["bounds"]
        self.terrain = world["terrain"]
        self.heights = array("h")
        self.heights.frombytes((path / "terrain.i16").read_bytes())
        if sys.byteorder != "little":
            self.heights.byteswap()
        self.index = {}
        for item in world["prisms"]:
            ring = item["ring"]
            xs, ys = ring[::2], ring[1::2]
            bounds = (min(xs), max(xs), min(ys), max(ys))
            prism = Prism(ring, item["holes"], item["base"], item["top"], bounds)
            for i in range(
                floor(bounds[0] / self.bucket), floor(bounds[1] / self.bucket) + 1
            ):
                for j in range(
                    floor(bounds[2] / self.bucket), floor(bounds[3] / self.bucket) + 1
                ):
                    self.index.setdefault((i, j), []).append(prism)
        self.width = ceil((b["maxX"] - b["minX"]) / self.cell)
        self.height = ceil((b["maxY"] - b["minY"]) / self.cell)
        self.walkable = {}
        # Resolve static building geometry before the first timed observation.
        for y in range(self.height):
            for x in range(self.width):
                self.open_cell((x, y))

    def ground(self, x, y):
        t = self.terrain
        fx = max(0.0, min(t["nx"] - 1.0001, (x - t["minX"]) / t["step"]))
        fy = max(0.0, min(t["ny"] - 1.0001, (y - t["minY"]) / t["step"]))
        i, j = int(fx), int(fy)
        index = j * t["nx"] + i
        a, b = fx - i, fy - j
        row = index + t["nx"]
        h = self.heights
        return t["scale"] * (
            (h[index] * (1 - a) + h[index + 1] * a) * (1 - b)
            + (h[row] * (1 - a) + h[row + 1] * a) * b
        )

    def nearby(self, x, y):
        return self.index.get((floor(x / self.bucket), floor(y / self.bucket)), ())

    def can_stand(self, x, y, z, radius=3.2, height=9.0):
        b = self.bounds
        if not (
            b["minX"] + radius < x < b["maxX"] - radius
            and b["minY"] + radius < y < b["maxY"] - radius
        ):
            return False
        if self.ground(x, y) > z + 1.15:
            return False
        for dx, dy in ((0, 0), (-radius, 0), (radius, 0), (0, -radius), (0, radius)):
            for prism in self.nearby(x + dx, y + dy):
                if (
                    prism.top > z + 0.9
                    and prism.bottom < z + height
                    and prism.contains(x + dx, y + dy)
                ):
                    return False
        return True

    def clear(self, start, end, arc=0.0):
        x, y, z = start
        dx, dy, dz = (end[i] - start[i] for i in range(3))
        count = max(2, ceil(hypot(dx, dy) / 3.0))
        for i in range(1, count):
            f = i / count
            px, py, pz = x + f * dx, y + f * dy, z + f * dz + arc * f * (1 - f)
            if self.ground(px, py) >= pz or any(
                p.bottom <= pz <= p.top and p.contains(px, py)
                for p in self.nearby(px, py)
            ):
                return False
        return True

    def grid(self, point):
        return (
            floor((point[0] - self.bounds["minX"]) / self.cell),
            floor((point[1] - self.bounds["minY"]) / self.cell),
        )

    def center(self, cell):
        return (
            self.bounds["minX"] + (cell[0] + 0.5) * self.cell,
            self.bounds["minY"] + (cell[1] + 0.5) * self.cell,
        )

    def open_cell(self, cell):
        if cell not in self.walkable:
            x, y = cell
            if not (0 <= x < self.width and 0 <= y < self.height):
                return False
            x, y = self.center(cell)
            self.walkable[cell] = self.can_stand(x, y, self.ground(x, y), radius=4.3)
        return self.walkable[cell]

    def nearest_open(self, cell):
        x, y = cell
        cells = [(x + dx, y + dy) for dx in range(-3, 4) for dy in range(-3, 4)]
        cells.sort(key=lambda c: (c[0] - x) ** 2 + (c[1] - y) ** 2)
        return next((c for c in cells if self.open_cell(c)), None)

    def path(self, start, end):
        a, b = self.nearest_open(self.grid(start)), self.nearest_open(self.grid(end))
        if a is None or b is None or a == b:
            return []
        distance = lambda c: hypot(c[0] - b[0], c[1] - b[1])
        queue = [(distance(a), 0.0, a)]
        parents, costs = {}, {a: 0.0}
        best = a
        for _ in range(750):
            if not queue:
                break
            _, cost, cell = heappop(queue)
            if cost != costs[cell]:
                continue
            if distance(cell) < distance(best):
                best = cell
            if cell == b:
                break
            x, y = cell
            for dx, dy, step in self.neighbors:
                dest = (x + dx, y + dy)
                if cost + step >= costs.get(dest, float("inf")) or not self.open_cell(
                    dest
                ):
                    continue
                if (
                    dx
                    and dy
                    and not (
                        self.open_cell((x + dx, y)) and self.open_cell((x, y + dy))
                    )
                ):
                    continue
                costs[dest] = cost + step
                parents[dest] = cell
                heappush(
                    queue, (cost + step + 1.20 * distance(dest), cost + step, dest)
                )
        result = []
        while best != a:
            result.append(self.center(best))
            best = parents[best]
        return result[::-1]

    def walk_clear(self, start, end, radius, height):
        x, y, z = start
        dx, dy = end[0] - x, end[1] - y
        count = max(1, ceil(hypot(dx, dy) / 3.0))
        previous = z
        for i in range(1, count + 1):
            px, py = x + dx * i / count, y + dy * i / count
            ground = self.ground(px, py)
            if ground > previous + 2.6:
                return False
            standing = max(ground, z - 0.7) if i < 3 else ground
            if not self.can_stand(px, py, standing, radius, height):
                return False
            previous = ground
        return True
