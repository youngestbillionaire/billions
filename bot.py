"""Original Mech Royale policy using only the kit's map and observations."""

import math
from time import perf_counter
from typing import ClassVar

import mechfight
from arena import Arena


def unit(x, y):
    speed = math.hypot(x, y)
    return (x / speed, y / speed) if speed > 0.001 else (0.0, 0.0)


def angle_delta(a, b):
    return math.remainder(a - b, math.tau)


def clamp(value, low, high):
    return min(high, max(low, value))


class Bot:
    name = "Billions"
    weapons: ClassVar = {"mortar": 0, "gun": 1, "lightning": 2, "laser": 3}
    upgrades: ClassVar = {
        "nanites": 10,
        "armor": 9,
        "plating": 8,
        "servos": 7,
        "jets": 6,
        "reactor": 5,
        "capacitor": 4,
        "laser_charge": 6,
        "laser_damage": 5,
        "gun_damage": 4,
        "gun_mag": 4,
        "gun_rate": 4,
        "gun_velocity": 3,
        "lightning_range": 5,
        "mortar_reload": 4,
        "gyros": 3,
        "sensors": 2,
    }
    offsets = (
        0,
        0.37,
        -0.37,
        0.75,
        -0.75,
        1.15,
        -1.15,
        1.55,
        -1.55,
        2.10,
        -2.10,
        math.pi,
    )

    def __init__(self, hello):
        self.config = hello["config"]
        self.arena = Arena(hello["map"]["dir"])
        self.dt = self.config["match"]["decide_every"] / self.config["match"]["tick_hz"]
        self.anchor = None
        self.route = []
        self.route_goal = None
        self.replan = -1.0
        self.last_damage = -100.0
        self.damage_bearing = None
        self.recovering = False
        self.cover = None
        self.cover_refresh = -1.0
        self.pickup_memory = {}
        self.armed = False
        self.enemy_health = {}
        self.enemy_conflicts = set()
        self.free_for_all = False
        self.last_position = None
        self.stuck = 0
        self.strafe = 1 if hello["seed"] % 2 else -1

    def act(self, obs):
        me = obs["self"]
        x, y, z = pos = me["pos"]
        t, zone = obs["t"], obs["zone"]
        stats = me["stats"]
        held = {w["kind"] for w in me["weapons"] if w}
        self.armed = bool(held.intersection({"laser", "lightning"}))
        self.remember_pickups(obs, held)
        hp = me["hp"] / max(1, me["max_hp"])
        muzzle = (x, y, z + stats["muzzle_height"])
        if self.anchor is None:
            self.anchor = math.atan2(
                y - zone["next_center"][1], x - zone["next_center"][0]
            )
        for event in obs["events"]:
            if event["type"] == "damaged" and event.get("bearing") is not None:
                self.last_damage, self.damage_bearing = t, event["bearing"]

        enemies = sorted(
            (m for m in obs["mechs"] if m["kind"] == "mech"),
            key=lambda m: math.dist(m["pos"][:2], pos[:2]),
        )
        self.observe_enemy_combat(obs, enemies)
        near = [m for m in enemies if math.dist(m["pos"][:2], pos[:2]) < 115]
        focus = self.visible_target(near or enemies, muzzle, hp)
        danger = t - self.last_damage < 2.2 or bool(near)
        crossfire = (
            self.free_for_all
            and sum(math.dist(e["pos"][:2], pos[:2]) < 78 for e in near) >= 2
        )
        if hp >= 0.90:
            self.recovering = False
        elif danger and (
            hp < (0.71 if self.free_for_all else 0.65) or len(near) >= 3 and hp < 0.80
        ):
            self.recovering = True
        next_center = zone["next_center"]
        next_range = math.dist(pos[:2], next_center)
        safe_radius = max(0.0, zone["next_radius"] - 16)
        travel = max(0.0, next_range - safe_radius) / max(1, stats["walk_speed"])
        urgent = not me["in_zone"] or (
            next_range > safe_radius and travel + 9 >= zone["shrink_end"] - t
        )
        retreat = (self.recovering or crossfire) and not urgent and obs["alive"] > 2
        if self.free_for_all and focus and obs["alive"] > 3:
            distance = math.dist(focus["pos"][:2], pos[:2])
            low = focus.get("hp", 1400) < 0.55 * focus.get("max_hp", 1400)
            bearing = math.atan2(y - focus["pos"][1], x - focus["pos"][0])
            distracted = (
                "aim" in focus and abs(angle_delta(bearing, focus["aim"][0])) > 1.05
            )
            if (
                distance > 48
                and not (low and hp > 0.60)
                and not (distracted and distance < 94 and not crossfire)
                and t - self.last_damage > 1.3
            ):
                focus = None
        goal, can_farm = self.destination(obs, hp, near, focus, urgent, retreat)
        act = {"tick": obs["tick"]}
        if obs.get("offer"):
            act["pick"] = max(
                range(len(obs["offer"]["choices"])),
                key=lambda i: self.upgrade_value(obs["offer"]["choices"][i]["id"]),
            )
        self.collect_weapon(obs, act)

        if not focus and can_farm:
            focus = self.farm_target(obs, muzzle)
        if retreat and focus and obs["alive"] > 3:
            close = math.dist(focus["pos"][:2], pos[:2]) < 38
            vulnerable = focus.get("hp", 1400) < 160
            if not close and not vulnerable:
                focus = None
        combat = self.choose_shot(focus, me, muzzle, retreat, obs["alive"])
        firing = combat[1] if combat else [False, False]
        aim = combat[0] if combat else self.look(obs, goal, near)

        step, stuck_jump = self.steer(obs, goal, urgent, retreat)
        speed = math.hypot(*step)
        energy = me["energy"]
        arming = (
            not self.armed
            and not danger
            and any(
                self.want_weapon(p["kind"], held) and math.dist(p["pos"][:2], goal) < 2
                for _, p in self.pickup_memory.values()
            )
        )
        boosting = speed > 0.35 and (
            (urgent and energy > 6)
            or (retreat and energy > 13)
            or (arming and energy > 52)
            or (self.last_damage + 1.5 > t and energy > 58)
        )
        if not me["grounded"] and not stuck_jump:
            boosting = False
        act.update(
            move=list(step), aim=list(aim), fire=firing, boost=boosting, jump=stuck_jump
        )
        if any(
            w and w["kind"] == "gun" and 0 < w["ammo"] < 4 and not w["reload"]
            for w in me["weapons"]
        ):
            act["reload"] = not bool(focus) and not danger
        return act

    def upgrade_value(self, name):
        value = self.upgrades.get(name, 0)
        if self.free_for_all and name in {"armor", "plating"}:
            value += 3
        return value

    def observe_enemy_combat(self, obs, enemies):
        if self.free_for_all:
            return
        t = obs["t"]
        zone = obs["zone"]
        own_hits = {
            event.get("target") for event in obs["events"] if event["type"] == "hit"
        }
        npcs = [other for other in obs["mechs"] if other["kind"] == "npc"]
        for enemy in enemies:
            if "hp" not in enemy:
                continue
            index = enemy["id"]
            previous = self.enemy_health.get(index)
            current = enemy["hp"]
            self.enemy_health[index] = (t, current)
            if (
                previous
                and t - previous[0] < 0.24
                and previous[1] - current >= 34
                and index not in own_hits
                and math.dist(enemy["pos"][:2], zone["center"]) < zone["radius"] - 15
                and all(
                    math.dist(enemy["pos"][:2], npc["pos"][:2]) > 55 for npc in npcs
                )
            ):
                self.enemy_conflicts.add(index)
        self.free_for_all = len(self.enemy_conflicts) >= 2

    def visible_target(self, enemies, muzzle, hp):
        candidates = []
        for enemy in enemies[:6]:
            x, y, z = enemy["pos"]
            distance = math.hypot(x - muzzle[0], y - muzzle[1])
            if distance >= 248:
                continue
            height = 4.5 * (1 + 0.05 * enemy.get("cubes", 0))
            if not self.arena.clear(muzzle, (x, y, z + height)):
                continue
            health = enemy.get("hp", 1400)
            prey = max(0, 1 - health / 1400)
            score = distance - (65 if hp > 0.55 else 35) * prey
            if enemy.get("charging") and distance < 65:
                score -= 22
            candidates.append((score, enemy["id"], enemy))
            if len(candidates) >= 3:
                break
        return min(candidates)[2] if candidates else None

    def destination(self, obs, hp, near, focus, urgent, retreat):
        me, zone = obs["self"], obs["zone"]
        x, y, z = me["pos"]
        center = zone["next_center"]
        radius = zone["next_radius"]
        t = obs["t"]
        edge = min(160, max(0, radius * 0.59 - 7))
        angle = self.anchor + self.strafe * (0.24 if near else 0.0)
        anchor = (
            center[0] + edge * math.cos(angle),
            center[1] + edge * math.sin(angle),
        )
        inward = math.dist((x, y), center)
        if urgent:
            if inward > max(8, radius - 38):
                return center, False
            return anchor, False
        drop_goal = self.supply_goal(obs, hp, near)
        if drop_goal is not None:
            return drop_goal, False

        if retreat:
            if self.last_damage + 2.2 < t and not near:
                return (x, y), False
            cover = self.find_cover(obs, near)
            if cover is not None:
                return cover, False
            ax, ay = 0.0, 0.0
            for enemy in near[:5]:
                px, py = enemy["pos"][:2]
                vx, vy = unit(x - px, y - py)
                weight = 1 / max(12, math.hypot(x - px, y - py))
                ax, ay = ax + vx * weight, ay + vy * weight
            if not near and self.damage_bearing is not None:
                ax, ay = -math.cos(self.damage_bearing), -math.sin(self.damage_bearing)
            ax, ay = unit(ax, ay)
            escape = (x + ax * 65, y + ay * 65)
            limit = max(0.0, zone["radius"] - 25)
            if math.dist(escape, zone["center"]) > limit:
                gx, gy = unit(
                    escape[0] - zone["center"][0], escape[1] - zone["center"][1]
                )
                escape = (
                    zone["center"][0] + limit * gx,
                    zone["center"][1] + limit * gy,
                )
            return escape, False

        if self.free_for_all:
            cubes = self.safe_cubes(obs, near)
            if cubes is not None:
                return cubes, False

        available = {w["kind"] for w in me["weapons"] if w}
        if not self.armed and not any(
            math.dist(e["pos"][:2], (x, y)) < (30 if hp > 0.7 else 40) for e in near
        ):
            arming = [
                (
                    math.dist(p["pos"][:2], (x, y)) - 7 * self.weapons[p["kind"]],
                    p["id"],
                    p,
                )
                for _, p in self.pickup_memory.values()
                if self.want_weapon(p["kind"], available)
                and math.dist(p["pos"][:2], (x, y)) < 126
                and abs(p["pos"][2] - z) < 5
                and math.dist(p["pos"][:2], center) < radius + 18
            ]
            if arming:
                return tuple(min(arming)[2]["pos"][:2]), False

        if focus and (distance := math.dist(focus["pos"][:2], (x, y))) < 90:
            ex, ey = focus["pos"][:2]
            ux, uy = unit(ex - x, ey - y)
            secondary = {w["kind"] for w in me["weapons"] if w}
            ideal = (
                34 if "lightning" in secondary else 57 if "laser" in secondary else 54
            )
            if hp > 0.73 and len(near) <= 2 and focus.get("hp", 1400) < 250:
                ideal = min(ideal, 31)
            radial = clamp((distance - ideal) / 27, -0.98, 0.75)
            sign = self.strafe * (1 if int(t / 2.7) % 2 else -1)
            return (
                x + (ux * radial - uy * sign) * 24,
                y + (uy * radial + ux * sign) * 24,
            ), False

        options = []
        for pool, value, category in (
            (obs["cubes"], 34, "cube"),
            ((p for _, p in self.pickup_memory.values()), 40, "weapon"),
            (obs["crates"], 29 if self.free_for_all else 11, "crate"),
        ):
            for item in pool:
                if category == "weapon" and not self.want_weapon(
                    item["kind"], available
                ):
                    continue
                px, py, pz = item["pos"]
                distance = math.hypot(px - x, py - y)
                if (
                    distance < 75
                    and abs(z - pz) < 6
                    and math.dist((px, py), center) < max(36, radius + 20)
                ):
                    if (
                        category == "crate"
                        and distance < 14
                        and hp > 0.70
                        and self.arena.clear(
                            (x, y, z + me["stats"]["muzzle_height"]), (px, py, pz + 2)
                        )
                    ):
                        return (x, y), True
                    options.append((distance - value, item["id"], (px, py)))
        if options:
            return min(options)[2], True
        if math.dist((x, y), anchor) < 12 and hp < 0.94 and not near:
            return (x, y), False
        if math.dist((x, y), anchor) < 12 and radius > 45:
            return (
                anchor[0] + 16 * math.cos(t / 10 + self.anchor),
                anchor[1] + 16 * math.sin(t / 10 + self.anchor),
            ), True
        return anchor, True

    def remember_pickups(self, obs, held):
        t = obs["t"]
        pos = obs["self"]["pos"]
        visible = {p["id"] for p in obs["pickups"]}
        for key, (seen, pickup) in list(self.pickup_memory.items()):
            if (
                t - seen > 24
                or not self.want_weapon(pickup["kind"], held)
                or (key not in visible and math.dist(pickup["pos"], pos) < 7)
            ):
                del self.pickup_memory[key]
        for pickup in obs["pickups"]:
            if self.want_weapon(pickup["kind"], held):
                self.pickup_memory[pickup["id"]] = (t, pickup)

    def safe_cubes(self, obs, enemies):
        x, y, z = obs["self"]["pos"]
        if any(math.dist(e["pos"][:2], (x, y)) < 25 for e in enemies):
            return None
        zone = obs["zone"]
        options = []
        for item in obs["cubes"]:
            px, py, pz = item["pos"]
            distance = math.hypot(px - x, py - y)
            if (
                distance > 42
                or abs(pz - z) > 5
                or math.dist((px, py), zone["next_center"])
                > max(4, zone["next_radius"] - 5)
                or any(math.dist(e["pos"][:2], (px, py)) < 33 for e in enemies)
            ):
                continue
            cluster = sum(
                math.dist(item["pos"], other["pos"]) <= 5 for other in obs["cubes"]
            )
            options.append((distance - min(5, cluster) * 4, item["id"], (px, py)))
        return min(options)[2] if options else None

    def supply_goal(self, obs, hp, threats):
        me, zone = obs["self"], obs["zone"]
        x, y, z = me["pos"]
        options = []
        for drop in obs["drops"]:
            px, py, pz = drop["pos"]
            distance = math.hypot(px - x, py - y)
            if (
                distance > (115 if hp < 0.9 else 78)
                or abs(z - pz) > 9
                or drop["lands_in"] > max(3, distance / 12)
                or math.dist((px, py), zone["next_center"])
                > max(6, zone["next_radius"] - 6)
                or any(math.dist(e["pos"][:2], (px, py)) < 31 for e in threats)
            ):
                continue
            if drop["holder"] == me["id"] and distance + 2 < drop["radius"]:
                return x, y
            options.append((distance, drop["id"], (px, py)))
        return min(options)[2] if options else None

    def find_cover(self, obs, enemies):
        me, zone, t = obs["self"], obs["zone"], obs["t"]
        x, y, z = me["pos"]
        if t < self.cover_refresh:
            return self.cover
        self.cover_refresh = t + 0.55
        enemies = [
            (e["pos"][0], e["pos"][1], e["pos"][2] + 7 * (1 + 0.05 * e.get("cubes", 0)))
            for e in enemies[:3]
        ]
        if not enemies and self.damage_bearing is not None:
            yaw = self.damage_bearing
            enemies = [(x + 44 * math.cos(yaw), y + 44 * math.sin(yaw), z + 7)]
        if not enemies:
            self.cover = None
            return None
        torso = me["stats"]["height"] * 0.58
        visible_now = sum(
            self.arena.clear(enemy, (x, y, z + torso)) for enemy in enemies
        )
        if visible_now == 0:
            self.cover = (x, y)
            return self.cover
        best = None
        deadline = perf_counter() + 0.006
        away = math.atan2(y - enemies[0][1], x - enemies[0][0])
        zone_limit = zone["radius"] - max(5.0, zone["dps"] * 0.075)
        for radius in (12, 25, 39):
            for offset in (0.0, -0.7, 0.7, -1.4, 1.4, -2.1, 2.1):
                if perf_counter() >= deadline:
                    break
                px = x + radius * math.cos(away + offset)
                py = y + radius * math.sin(away + offset)
                if math.dist((px, py), zone["center"]) >= zone_limit:
                    continue
                pz = self.arena.ground(px, py)
                if not self.arena.can_stand(
                    px, py, pz, me["stats"]["radius"], me["stats"]["height"]
                ):
                    continue
                exposed = sum(
                    self.arena.clear(enemy, (px, py, pz + torso)) for enemy in enemies
                )
                if exposed >= visible_now:
                    continue
                progress = math.dist((px, py), zone["next_center"]) - math.dist(
                    (x, y), zone["next_center"]
                )
                score = 24 * exposed + 0.45 * radius + 0.16 * max(0, progress)
                if best is None or score < best[0]:
                    best = (score, (px, py))
            if best and best[0] < 14:
                break
        self.cover = best[1] if best else None
        return self.cover

    def want_weapon(self, kind, held):
        return (
            kind not in held
            and self.weapons.get(kind, 0) > 1
            and (
                len(held) < 2
                or self.weapons[kind] > min(self.weapons.get(k, 0) for k in held)
            )
        )

    def collect_weapon(self, obs, act):
        me = obs["self"]
        held = {w["kind"] for w in me["weapons"] if w}
        reach = self.config["loot"]["pickup_radius"]
        eligible = [
            p
            for p in obs["pickups"]
            if self.want_weapon(p["kind"], held)
            and math.dist(p["pos"][:2], me["pos"][:2]) <= reach
            and abs(p["pos"][2] - me["pos"][2]) <= 4
        ]
        if not eligible:
            return
        item = max(eligible, key=lambda p: self.weapons[p["kind"]])
        if None not in me["weapons"]:
            slots = [i for i, w in enumerate(me["weapons"]) if not w.get("charge")]
            if not slots:
                return
            replacement = min(
                slots, key=lambda i: self.weapons[me["weapons"][i]["kind"]]
            )
            if (
                self.weapons[me["weapons"][replacement]["kind"]]
                >= self.weapons[item["kind"]]
            ):
                return
            act["drop"] = replacement
        act["take"] = item["id"]

    def farm_target(self, obs, muzzle):
        me = obs["self"]
        if obs["t"] - self.last_damage < 4 or me["hp"] < 0.78 * me["max_hp"]:
            return None
        crates = sorted(obs["crates"], key=lambda c: math.dist(c["pos"], muzzle))
        for crate in crates[:3]:
            target = (*crate["pos"][:2], crate["pos"][2] + 2.0)
            if math.dist(target, muzzle) < 76 and self.arena.clear(muzzle, target):
                return {**crate, "kind": "crate", "vel": (0, 0, 0)}
        drones = (e for e in obs["mechs"] if e["kind"] == "npc")
        return next((e for e in drones if math.dist(e["pos"], muzzle) < 33), None)

    def choose_shot(self, target, me, muzzle, retreat, alive):
        weapons = me["weapons"]
        if not target:
            charged = [
                bool(
                    w
                    and w["kind"] == "laser"
                    and w.get("charge", 0) < 0.1
                    and w.get("charge", 0) > 0
                )
                for w in weapons
            ]
            return (me["aim"], charged) if any(charged) else None
        kind = target["kind"]
        center_height = (
            2.0
            if kind in {"npc", "crate"}
            else 4.5 * (1 + 0.05 * target.get("cubes", 0))
        )
        point = (*target["pos"][:2], target["pos"][2] + center_height)
        limits = me["stats"]
        current = me["aim"]
        candidates = []
        for index, weapon in enumerate(weapons):
            if weapon is None:
                continue
            kind_w = weapon["kind"]
            stats = weapon["stats"]
            charging = kind_w == "laser" and weapon.get("charge", 0) > 0
            if weapon["cooldown"] > 0.32 and not charging:
                continue
            if kind_w == "gun" and (weapon["reload"] > 0.02 or not weapon["ammo"]):
                continue
            if kind_w == "lightning" and me["energy"] < stats["energy"] + (
                14 if retreat else 3
            ):
                continue
            prediction = self.aim_at(
                kind_w, stats, point, target["vel"], muzzle, center_height
            )
            if prediction is None:
                continue
            yaw, pitch, distance, tolerance = prediction
            if not limits["min_pitch"] <= pitch <= limits["max_pitch"]:
                continue
            yaw_error = abs(angle_delta(yaw, current[0]))
            pitch_error = abs(pitch - current[1])
            aim_error = max(yaw_error, pitch_error)
            aligned = (
                yaw_error < tolerance + limits["turn_rate"] * self.dt * 0.38
                and pitch_error < tolerance + limits["pitch_rate"] * self.dt * 0.38
            )
            ready = weapon["cooldown"] <= max(0.0, self.dt - 0.015)
            fire = ready and aligned
            score = {"gun": 1.8, "lightning": 2.75, "laser": 2.65, "mortar": 2.2}[
                kind_w
            ]
            if kind_w == "laser":
                charge = weapon.get("charge", 0)
                if retreat and not charging and alive > 3:
                    continue
                if charge >= stats["charge"] - self.dt * 0.3:
                    fire = not (
                        aligned or aim_error < 0.09 and charge > stats["charge"] + 0.25
                    )
                else:
                    fire = (charging or ready) and (charging or aim_error < 0.45)
                if charging:
                    score += 4
                if "mortar" in target.get("weapons", ()):
                    score += 0.45
            elif kind_w == "mortar" and distance < 22:
                continue
            if kind == "crate" and "gun" == kind_w:
                score += 0.35
            score += 0.30 * ready - 0.35 * min(2.0, aim_error)
            candidates.append((score, index, yaw, pitch, fire, tolerance))
        if not candidates:
            dx, dy, dz = (point[i] - muzzle[i] for i in range(3))
            return (
                math.atan2(dy, dx),
                clamp(
                    math.atan2(dz, math.hypot(dx, dy)),
                    limits["min_pitch"],
                    limits["max_pitch"],
                ),
            ), [False, False]
        _, index, yaw, pitch, trigger, _ = max(candidates)
        fire = [False, False]
        fire[index] = trigger
        for _, other, other_yaw, other_pitch, wants_fire, tol in candidates:
            if other != index and weapons[other]["kind"] != "laser":
                fire[other] = (
                    wants_fire
                    and max(abs(angle_delta(yaw, other_yaw)), abs(pitch - other_pitch))
                    < tol
                )
        return (yaw, pitch), fire

    def aim_at(self, kind, stats, point, velocity, muzzle, height):
        dx, dy, dz = (point[i] - muzzle[i] for i in range(3))
        distance = math.hypot(dx, dy)
        travel = 0.045
        if kind == "gun":
            for _ in range(3):
                travel = (
                    math.hypot(dx + velocity[0] * travel, dy + velocity[1] * travel)
                    - 2.5
                ) / stats["speed"] + 0.045
            if math.hypot(distance, dz) > stats["range"] * 0.96:
                return None
        elif kind == "mortar":
            speed, gravity = stats["speed"], stats["gravity"]
            if not 20 < distance < min(148, 0.78 * speed * speed / gravity):
                return None
            trajectory = max(0.1, (distance - 3) / speed)
            for _ in range(4):
                px = dx + velocity[0] * trajectory
                py = dy + velocity[1] * trajectory
                pz = dz - height + 0.6 + 0.5 * gravity * trajectory * trajectory
                trajectory = (math.sqrt(px * px + py * py + pz * pz) - 3) / speed
            end = (muzzle[0] + px, muzzle[1] + py, point[2] - height + 0.6)
            if not self.arena.clear(
                muzzle, end, 0.5 * gravity * trajectory * trajectory
            ):
                return None
            return (
                math.atan2(py, px),
                math.atan2(pz, math.hypot(px, py)),
                distance,
                0.047,
            )
        elif math.hypot(distance, dz) > stats["range"] * 0.97:
            return None
        px, py, pz = (point[i] + velocity[i] * travel for i in range(3))
        if not self.arena.clear(muzzle, (px, py, pz)):
            return None
        dx, dy, dz = px - muzzle[0], py - muzzle[1], pz - muzzle[2]
        tolerance = clamp(2.1 / max(12, distance), 0.012, 0.13)
        if kind == "lightning":
            tolerance = math.radians(stats["cone_deg"]) * 0.41
        return (
            math.atan2(dy, dx),
            math.atan2(dz, math.hypot(dx, dy)),
            distance,
            tolerance,
        )

    def look(self, obs, goal, near):
        x, y = obs["self"]["pos"][:2]
        t = obs["t"]
        if near:
            ex, ey = near[0]["pos"][:2]
            return math.atan2(ey - y, ex - x), 0.0
        if t - self.last_damage < 2.7 and self.damage_bearing is not None:
            return self.damage_bearing, 0.0
        tactical = {"laser_charge", "lightning", "gun", "mortar", "jet", "boost"}
        for noise in obs["sounds"]:
            if noise["kind"] in tactical and noise["dist"] == "near":
                return noise["bearing"], 0.0
        yaw = math.atan2(goal[1] - y, goal[0] - x)
        if not self.armed and not self.pickup_memory:
            return math.remainder(yaw + 1.34 * math.sin(t * 1.65), math.tau), 0.0
        return math.remainder(yaw + 0.68 * math.sin(t * 1.4), math.tau), 0.0

    def steer(self, obs, goal, urgent, retreat):
        me, t = obs["self"], obs["t"]
        x, y, z = pos = me["pos"]
        radius, height = me["stats"]["radius"], me["stats"]["height"]
        distance = math.dist((x, y), goal)
        if (
            self.last_position is not None
            and distance > 5
            and math.dist(pos[:2], self.last_position) < 0.25
        ):
            self.stuck += 1
        else:
            self.stuck = 0
        self.last_position = (x, y)
        direction = goal
        if distance > 12 and not self.arena.walk_clear(
            pos,
            (
                x + (goal[0] - x) * min(1, 36 / distance),
                y + (goal[1] - y) * min(1, 36 / distance),
            ),
            radius,
            height,
        ):
            if t >= self.replan and (
                not self.route
                or math.dist(self.route_goal, goal) > 17
                or self.stuck >= 4
            ):
                self.route = self.arena.path(pos, goal)
                self.route_goal = goal
                self.replan = t + 0.45
            while self.route and math.dist((x, y), self.route[0]) < 4.4:
                self.route.pop(0)
            if self.route and math.dist((x, y), self.route[0]) < 20:
                direction = self.route[0]
        else:
            self.route.clear()
        yaw = math.atan2(direction[1] - y, direction[0] - x)
        speed = me["stats"]["boost_speed" if urgent or retreat else "walk_speed"]
        projectiles = [
            p
            for p in obs["projectiles"]
            if math.dist(p["pos"], (x, y, z + height / 2)) < 51
        ][:12]
        if distance <= 1 and not projectiles:
            return (0.0, 0.0), False
        best = (float("-inf"), (0.0, 0.0))
        for offset in self.offsets if me["grounded"] else self.offsets[:7]:
            heading = yaw + offset
            ux, uy = math.cos(heading), math.sin(heading)
            length = min(8.0, max(3.5, speed * 0.45))
            check_x, check_y = x + length * ux, y + length * uy
            stand = max(self.arena.ground(check_x, check_y), z - 0.5)
            fits = self.arena.can_stand(check_x, check_y, stand, radius, height)
            if fits and not self.arena.can_stand(
                x + 2 * ux, y + 2 * uy, z, radius, height
            ):
                fits = False
            target_value = 3.6 * math.cos(offset) + (
                0.06 * self.strafe if offset > 0 else 0.0
            )
            danger = self.projectile_risk(
                projectiles, (x, y, z + height * 0.5), (ux * speed, uy * speed), radius
            )
            if not fits:
                target_value -= 5.0
            score = target_value - danger
            if score > best[0]:
                best = (score, (ux, uy))
        jump = me["grounded"] and self.stuck >= 5 and me["energy"] > 40 and distance > 9
        scale = max(0.32, min(1.0, distance / 9))
        return (best[1][0] * scale, best[1][1] * scale), jump

    def projectile_risk(self, projectiles, position, velocity, radius):
        risk = 0.0
        for projectile in projectiles:
            dx, dy, dz = (projectile["pos"][i] - position[i] for i in range(3))
            vx = projectile["vel"][0] - velocity[0]
            vy = projectile["vel"][1] - velocity[1]
            moment = -(dx * vx + dy * vy) / max(1.0, vx * vx + vy * vy)
            if not 0 < moment < 0.45:
                continue
            if abs(dz + projectile["vel"][2] * moment) > 7:
                continue
            miss = math.hypot(dx + vx * moment, dy + vy * moment)
            if miss < radius + 3:
                risk += 5 * (1.0 - miss / (radius + 3))
        return risk


if __name__ == "__main__":
    mechfight.run(Bot)
