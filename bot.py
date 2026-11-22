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
        self.sim_step = 1 / self.config["match"]["tick_hz"]
        self.dt = self.config["match"]["decide_every"] * self.sim_step
        self.anchor = None
        self.route = []
        self.route_goal = None
        self.replan = -1.0
        self.last_damage = -100.0
        self.last_combat = -100.0
        self.damage_bearing = None
        self.attacker_id = None
        self.hits_from = {}
        self.contacts = {}
        self.short_target = None
        self.short_until = -1.0
        self.recovery_exit = None
        self.peek = None
        self.peek_after = -1.0
        self.finish_shot_after = -1.0
        self.finish_shot_target = None
        self.pursuit_until = -1.0
        self.pursuit_used = None
        self.pursuit_anchor = None
        self.previous_focus = None
        self.focus_id = None
        self.laser_blocked_until = -1.0
        self.last_laser_aim = None
        self.last_laser_target_time = -1.0
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
            if event["type"] == "kill":
                self.contacts.pop(event.get("target"), None)
                if self.previous_focus and self.previous_focus[1]["id"] == event.get(
                    "target"
                ):
                    self.previous_focus = None
            if event["type"] in {"damaged", "hit"}:
                self.last_combat = t
            if event["type"] == "damaged" and event.get("bearing") is not None:
                self.last_damage, self.damage_bearing = t, event["bearing"]
                self.attacker_id = event.get("by")
                if self.attacker_id is not None and 0 <= self.attacker_id < 16:
                    self.hits_from[self.attacker_id] = t
            if event["type"] == "hit" and event.get("effect") == "short":
                self.short_target = event.get("target")
                self.short_until = t + min(
                    1.1, self.config["weapons"]["lightning"]["short_s"]
                )
            if event["type"] == "effect" and event.get("effect") == "short":
                self.laser_blocked_until = (
                    t + self.config["weapons"]["lightning"]["short_s"]
                )

        enemies = sorted(
            (m for m in obs["mechs"] if m["kind"] == "mech"),
            key=lambda m: math.dist(m["pos"][:2], pos[:2]),
        )
        self.observe_enemy_combat(obs, enemies)
        near = [m for m in enemies if math.dist(m["pos"][:2], pos[:2]) < 115]
        self.contacts = {
            key: entry for key, entry in self.contacts.items() if t - entry[0] <= 1.4
        }
        self.contacts.update((enemy["id"], (t, enemy)) for enemy in near)
        threats = self.recent_threats(near, t)
        charging = any(
            w and w["kind"] == "laser" and w.get("charge", 0) > 0 for w in me["weapons"]
        )
        focus = self.visible_target(
            near or enemies,
            muzzle,
            hp,
            me["aim"][0],
            t,
            charging,
            next((w for w in me["weapons"] if w and w["kind"] == "lightning"), None),
            me["energy"],
        )
        new_attacker = (
            self.attacker_id != self.focus_id
            and sum(t - damage_at <= 0.85 for damage_at in self.hits_from.values()) >= 2
        )
        if new_attacker and t - self.last_damage <= 0.35 and not charging:
            attacker = next(
                (enemy for enemy in near if enemy["id"] == self.attacker_id), None
            )
            if (
                attacker
                and self.target_clear(muzzle, attacker)
                and (focus is None or focus.get("hp", 1400) > 70)
            ):
                focus = attacker
                self.cover_refresh = -1.0
        elif hp >= 0.72 and not charging and t < self.short_until:
            target = next(
                (enemy for enemy in near if enemy["id"] == self.short_target), None
            )
            if (
                target
                and math.dist(target["pos"][:2], pos[:2]) < 68
                and self.target_clear(muzzle, target)
            ):
                focus = target
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
        self.focus_id = focus["id"] if focus else None
        goal, can_farm = self.destination(
            obs, hp, near, focus, urgent, retreat, threats
        )
        if (
            urgent
            or hp < 0.69
            or new_attacker
            or (not retreat and self.peek and not self.peek[4])
        ):
            self.peek = None
        goal, peek_target = self.recovery_peek(
            obs,
            goal,
            hp,
            threats,
            focus,
            (retreat or self.peek is not None)
            and not urgent
            and not new_attacker
            and not charging,
        )
        if not retreat and not urgent and not focus and self.peek is None:
            chase_goal = self.corner_pursuit(obs, hp, threats)
            if chase_goal is not None:
                goal, can_farm = chase_goal, False
        if focus:
            self.previous_focus = (t, focus)
            self.pursuit_until = -1.0
            self.pursuit_used = None
            self.pursuit_anchor = None
        if (
            retreat
            and focus
            and focus["id"] == self.short_target
            and t < self.short_until
            and hp < 0.62
            and math.dist(pos[:2], goal) < 2
            and self.recovery_exit
            and self.retreat_zone_margin(obs, self.recovery_exit, 1.5) > 5
        ):
            goal = self.recovery_exit
        act = {"tick": obs["tick"]}
        if obs.get("offer"):
            act["pick"] = max(
                range(len(obs["offer"]["choices"])),
                key=lambda i: self.upgrade_value(obs["offer"]["choices"][i]["id"]),
            )
        self.collect_weapon(obs, act)

        if not focus and can_farm:
            focus = self.farm_target(obs, muzzle)
        last_shot = None
        if retreat and focus and obs["alive"] > 3:
            close = math.dist(focus["pos"][:2], pos[:2]) < 38
            vulnerable = focus.get("hp", 1400) < 160
            under_fire = (
                not self.free_for_all
                and t - self.last_damage <= 0.65
                and focus["id"] == self.attacker_id
                and math.dist(focus["pos"][:2], pos[:2]) < 125
                and any(w and w["kind"] == "gun" for w in me["weapons"])
            )
            if vulnerable and not (close or under_fire) and self.peek is None:
                last_shot = self.finishing_gun(obs, focus, muzzle, goal)
            safe_finish = vulnerable and (
                t >= self.finish_shot_after or focus["id"] != self.finish_shot_target
            )
            if not (close or under_fire or safe_finish) or (
                self.peek is not None and not peek_target
            ):
                focus = None
        if self.peek is not None:
            combat = (
                self.aligned_gun(me, peek_target, muzzle)
                if peek_target and not charging
                else None
            )
        else:
            combat = self.choose_shot(
                focus, me, muzzle, retreat, obs["alive"], t, urgent
            )
        if last_shot is not None and not charging:
            combat = last_shot
            self.finish_shot_after = t + 1.15
            self.finish_shot_target = self.focus_id
        if peek_target and combat and any(combat[1]):
            anchor, point, target_id, _, _ = self.peek
            self.peek = anchor, point, target_id, t + 1.3, True
            self.peek_after = t + 4.0
            goal = anchor
        firing = combat[1] if combat else [False, False]
        aim = combat[0] if combat else self.look(obs, goal, near)
        if (
            (not self.free_for_all or new_attacker)
            and not any(firing)
            and not charging
            and t - self.last_damage < 0.45
            and self.damage_bearing is not None
            and abs(angle_delta(self.damage_bearing, aim[0])) > 1.55
        ):
            aim = (self.damage_bearing, aim[1])

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
        if self.peek is not None and not self.peek[4]:
            boosting = False
        act.update(
            move=list(step), aim=list(aim), fire=firing, boost=boosting, jump=stuck_jump
        )
        if (
            not focus
            and self.peek is None
            and self.safe_reload(obs, near, muzzle, act, danger, goal)
        ):
            act["reload"] = True
        return act

    def safe_reload(self, obs, near, muzzle, act, danger, goal=None):
        me = obs["self"]
        for slot, gun in enumerate(me["weapons"]):
            if not gun or gun["kind"] != "gun":
                continue
            if act["fire"][slot] or act.get("drop") == slot or gun["reload"]:
                return False
            ammo = gun["ammo"]
            if 0 < ammo < 4 and not danger:
                return True
            if not 0 < ammo <= gun["stats"]["mag"] * 2 // 3:
                return False
            break
        else:
            return False
        if not me["in_zone"] or obs["t"] - self.last_damage < 1.9:
            return False
        if any(
            noise["dist"] == "near"
            and noise["kind"] in {"laser_charge", "lightning", "gun", "mortar", "boost"}
            for noise in obs["sounds"]
        ):
            return False
        closing_distance = me["stats"]["boost_speed"] * gun["stats"]["reload"] + 7
        if goal is not None and self.reload_on_rotation(
            obs, goal, self.recent_threats(near, obs["t"]), muzzle, gun
        ):
            return True
        if ammo > gun["stats"]["mag"] // 2:
            return False
        for enemy in near[:3]:
            ex, ey, ez = enemy["pos"]
            if math.dist((ex, ey), muzzle[:2]) < closing_distance or self.arena.clear(
                (ex, ey, ez + 7), muzzle
            ):
                return False
        return True

    def target_clear(self, muzzle, target):
        x, y, z = target["pos"]
        return self.arena.clear(
            muzzle, (x, y, z + 4.5 * (1 + 0.05 * target.get("cubes", 0)))
        )

    def recent_threats(self, visible, now):
        threats = list(visible)
        seen = {enemy["id"] for enemy in visible}
        hidden = sorted(self.contacts.values(), key=lambda contact: -contact[0])
        for seen_at, enemy in hidden:
            if enemy["id"] in seen or now - seen_at > 1.4 or len(threats) >= 4:
                continue
            age = min(0.65, now - seen_at)
            pos = enemy["pos"]
            vel = enemy["vel"]
            forecast = (pos[0] + age * vel[0], pos[1] + age * vel[1], pos[2])
            if not self.arena.clear(
                (*pos[:2], pos[2] + 7), (*forecast[:2], forecast[2] + 7)
            ):
                forecast = pos
            threats.append({**enemy, "pos": forecast})
        return threats

    def reload_on_rotation(self, obs, goal, visible, muzzle, gun):
        me = obs["self"]
        t = obs["t"]
        if not me["grounded"] or t - self.last_combat < 2.2 or not visible:
            return False
        distance = math.dist(me["pos"][:2], goal)
        segment = me["stats"]["boost_speed"] * (gun["stats"]["reload"] + 0.15)
        if distance < segment:
            return False
        ux, uy = unit(goal[0] - muzzle[0], goal[1] - muzzle[1])
        end = (muzzle[0] + segment * ux, muzzle[1] + segment * uy)
        if not self.arena.walk_clear(
            me["pos"], end, me["stats"]["radius"], me["stats"]["height"]
        ):
            return False
        for enemy in visible[:4]:
            ex, ey, ez = enemy["pos"]
            eye = ex, ey, ez + 7
            if math.dist(eye[:2], muzzle[:2]) < segment + 12:
                return False
            for fraction in (0, 0.33, 0.67, 1):
                x, y = (
                    muzzle[0] + segment * ux * fraction,
                    muzzle[1] + segment * uy * fraction,
                )
                if self.arena.clear(
                    eye, (x, y, self.arena.ground(x, y) + me["stats"]["muzzle_height"])
                ):
                    return False
        return True

    def recovery_peek(self, obs, goal, hp, threats, focus, allowed):
        me, t = obs["self"], obs["t"]
        pos = me["pos"]
        if not allowed or hp < 0.69:
            return goal, None
        if self.peek:
            anchor, point, target_id, expires, returning = self.peek
            if returning:
                if t >= expires or math.dist(pos[:2], anchor) < 1.1:
                    self.peek = None
                    return goal, None
                return anchor, None
            if t >= expires or t - self.last_damage < 0.45:
                self.peek = anchor, point, target_id, t + 1.3, True
                self.peek_after = t + 4.0
                return anchor, None
            if focus and focus["id"] == target_id:
                return point, focus
            return point, None

        if (
            not me["grounded"]
            or not 0.80 <= hp < 0.90
            or t < self.peek_after
            or t - self.last_combat <= me["stats"]["regen_delay"]
            or math.dist(pos[:2], goal) > 1.5
            or not self.recovery_exit
            or len(threats) != 1
            or self.retreat_zone_margin(obs, pos[:2], 3) < 8
            or not any(
                w
                and w["kind"] == "gun"
                and w["ammo"]
                and not w["reload"]
                and w["cooldown"] < 0.15
                for w in me["weapons"]
            )
        ):
            return goal, None
        target = threats[0]
        if not 24 < math.dist(pos[:2], target["pos"][:2]) < 80:
            return goal, None
        stats = me["stats"]
        current = (*pos[:2], pos[2] + stats["muzzle_height"])
        if self.target_clear(current, target):
            return goal, None
        bearing = math.atan2(target["pos"][1] - pos[1], target["pos"][0] - pos[0])
        for offset in (1.05, -1.05, 1.65, -1.65):
            x, y = (
                pos[0] + 5 * math.cos(bearing + offset),
                pos[1] + 5 * math.sin(bearing + offset),
            )
            if self.arena.walk_clear(
                pos, (x, y), stats["radius"], stats["height"]
            ) and self.target_clear(
                (x, y, self.arena.ground(x, y) + stats["muzzle_height"]), target
            ):
                self.peek = tuple(pos[:2]), (x, y), target["id"], t + 0.85, False
                return (x, y), None
        return goal, None

    def finishing_gun(self, obs, target, muzzle, cover):
        me, t = obs["self"], obs["t"]
        if (
            t < self.finish_shot_after
            or math.dist(me["pos"][:2], cover) <= 2
            or math.dist(me["pos"][:2], target["pos"][:2]) > 85
        ):
            return None
        return self.aligned_gun(me, target, muzzle, lethal=True)

    def aligned_gun(self, me, target, muzzle, lethal=False):
        cubes = self.config["cubes"]
        multiplier = 1 + cubes["damage_per_cube"] * min(me["cubes"], cubes["stat_cap"])
        for slot, gun in enumerate(me["weapons"]):
            if (
                not gun
                or gun["kind"] != "gun"
                or not gun["ammo"]
                or gun["reload"]
                or gun["cooldown"] > 0.04
            ):
                continue
            if (
                lethal
                and target.get("hp", math.inf)
                > gun["stats"]["damage"] * multiplier * 0.72
            ):
                continue
            center = 4.5 * (1 + 0.05 * target.get("cubes", 0))
            x, y, z = target["pos"]
            solution = self.aim_at(
                "gun", gun["stats"], (x, y, z + center), target["vel"], muzzle, center
            )
            if solution:
                yaw, pitch, _, tolerance = solution
                if (
                    abs(angle_delta(yaw, me["aim"][0])) <= tolerance
                    and abs(pitch - me["aim"][1]) <= tolerance
                ):
                    firing = [False, False]
                    firing[slot] = True
                    return (yaw, pitch), firing
        return None

    def corner_pursuit(self, obs, hp, threats):
        if self.previous_focus is None or hp < 0.84 or not obs["self"]["grounded"]:
            return None
        seen_at, target = self.previous_focus
        t, me = obs["t"], obs["self"]
        if (
            t - self.last_damage < 1.2
            or target.get("hp", 1400) > 115
            or any(enemy["id"] != target["id"] for enemy in threats)
            or any(drop["holder"] == me["id"] for drop in obs["drops"])
            or not any(
                w and w["kind"] == "gun" and w["ammo"] > 0 and not w["reload"]
                for w in me["weapons"]
            )
        ):
            return None
        if self.pursuit_used != seen_at and t - seen_at <= 0.2:
            self.pursuit_until = t + 0.65
            self.pursuit_used = seen_at
            self.pursuit_anchor = tuple(me["pos"][:2])
        if t >= self.pursuit_until:
            return None
        origin = me["pos"]
        if not 18 < math.dist(origin[:2], target["pos"][:2]) < 57:
            return None
        bearing = math.atan2(target["pos"][1] - origin[1], target["pos"][0] - origin[0])
        stats = me["stats"]
        for offset in (0.75, -0.75, 1.3, -1.3):
            x, y = (
                origin[0] + 7 * math.cos(bearing + offset),
                origin[1] + 7 * math.sin(bearing + offset),
            )
            z = self.arena.ground(x, y)
            if (
                self.retreat_zone_margin(obs, (x, y), 3.0) > 12
                and self.arena.walk_clear(
                    origin, (x, y), stats["radius"], stats["height"]
                )
                and self.arena.walk_clear(
                    (x, y, z), self.pursuit_anchor, stats["radius"], stats["height"]
                )
                and self.target_clear((x, y, z + stats["muzzle_height"]), target)
            ):
                return x, y
        return None

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

    def visible_target(self, enemies, muzzle, hp, yaw, t, charging, lightning, energy):
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
            bearing = math.atan2(y - muzzle[1], x - muzzle[0])
            score += 12 * abs(angle_delta(bearing, yaw))
            if enemy["id"] == self.focus_id:
                score -= 17 if charging else 7
            if enemy["id"] == self.attacker_id and t - self.last_damage < 0.8:
                score -= 11
            if enemy.get("charging") and distance < 65:
                score -= 22
                if (
                    lightning
                    and lightning["cooldown"] <= self.dt
                    and energy >= lightning["stats"]["energy"] + 5
                    and distance < lightning["stats"]["range"] - 3
                    and abs(angle_delta(bearing, yaw))
                    < math.radians(lightning["stats"]["cone_deg"]) * 0.4
                ):
                    score -= 32
            candidates.append((score, enemy["id"], enemy))
            if len(candidates) >= 3:
                break
        return min(candidates)[2] if candidates else None

    def destination(self, obs, hp, near, focus, urgent, retreat, threats):
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
            backup = self.use_recovery_exit(obs, threats)
            if backup is not None:
                return backup, False
            cover = self.find_cover(obs, threats)
            if cover is not None:
                cube = self.cube_near_retreat_path(obs, cover, near)
                return cube if cube is not None else cover, False
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
            return self.reachable_escape(obs, escape, near), False

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

    def cube_near_retreat_path(self, obs, cover, threats):
        me = obs["self"]
        origin = me["pos"]
        normalized = unit(cover[0] - origin[0], cover[1] - origin[1])
        remaining = math.dist(origin[:2], cover)
        if remaining < 4 or any(
            math.dist(enemy["pos"][:2], origin[:2]) < 35 for enemy in threats
        ):
            return None

        stats = me["stats"]
        candidates = []
        for cube in obs["cubes"]:
            x, y, z = cube["pos"]
            dx, dy = x - origin[0], y - origin[1]
            along = dx * normalized[0] + dy * normalized[1]
            across = abs(dx * normalized[1] - dy * normalized[0])
            if (
                3.6 < along < min(22, remaining + 1)
                and across < 3.0
                and abs(z - origin[2]) < 4.0
            ):
                candidates.append((along, cube["id"], (x, y)))
        for _, _, point in sorted(candidates)[:2]:
            if self.arena.walk_clear(origin, point, stats["radius"], stats["height"]):
                return point
        return None

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
            destination = (px, py)
            if (
                drop["holder"] is None
                and drop["lands_in"] <= 0.4
                and threats
                and me["grounded"]
            ):
                destination = (
                    self.supply_ring_position(obs, drop, threats) or destination
                )
            options.append((distance, drop["id"], destination))
        return min(options)[2] if options else None

    def supply_ring_position(self, obs, drop, threats):
        me, stats = obs["self"], obs["self"]["stats"]
        origin = me["pos"]
        px, py, pz = drop["pos"]
        distance = max(0, drop["radius"] - 2.8)
        if distance < 1 or math.dist(origin[:2], (px, py)) > 34:
            return None
        eyes = [(*e["pos"][:2], e["pos"][2] + 7) for e in threats[:2]]
        center = (px, py, pz + stats["height"] * 0.58)
        exposed = sum(self.arena.clear(eye, center) for eye in eyes)
        if not exposed:
            return None
        bearing = math.atan2(origin[1] - py, origin[0] - px)
        for offset in (0, 0.85, -0.85, 1.65, -1.65):
            x, y = (
                px + distance * math.cos(bearing + offset),
                py + distance * math.sin(bearing + offset),
            )
            z = self.arena.ground(x, y)
            if (
                self.retreat_zone_margin(obs, (x, y), 5) > 5
                and sum(
                    self.arena.clear(eye, (x, y, z + stats["height"] * 0.58))
                    for eye in eyes
                )
                < exposed
                and self.arena.walk_clear(
                    origin, (x, y), stats["radius"], stats["height"]
                )
                and self.cover_exit(obs, (x, y, z), eyes, 5) is not None
            ):
                return x, y
        return None

    def retreat_zone_margin(self, obs, point, eta):
        t, zone = obs["t"], obs["zone"]
        begin = max(t, zone["shrink_start"])
        span = max(0.1, zone["shrink_end"] - begin)
        fraction = clamp((t + eta - begin) / span, 0.0, 1.0)
        center = tuple(
            current + fraction * (future - current)
            for current, future in zip(zone["center"], zone["next_center"])
        )
        radius = zone["radius"] + fraction * (zone["next_radius"] - zone["radius"])
        return radius - math.dist(point, center)

    def reachable_escape(self, obs, escape, enemies):
        me = obs["self"]
        if not me["grounded"]:
            return escape
        x, y, z = me["pos"]
        stats = me["stats"]
        angle = math.atan2(escape[1] - y, escape[0] - x)
        center = obs["zone"]["next_center"]
        inward = math.atan2(center[1] - y, center[0] - x)
        present_margin = self.retreat_zone_margin(obs, (x, y), 0.0)
        best = None
        for distance in (min(26, math.dist((x, y), escape)), 11):
            if distance < 3:
                continue
            for heading in (
                angle,
                angle + 0.65,
                angle - 0.65,
                angle + 1.3,
                angle - 1.3,
                inward,
            ):
                point = (
                    x + distance * math.cos(heading),
                    y + distance * math.sin(heading),
                )
                eta = distance / stats["walk_speed"] + 0.4
                margin = self.retreat_zone_margin(obs, point, eta)
                if margin < max(0, min(7, present_margin - 1)):
                    continue
                if not self.arena.walk_clear(
                    (x, y, z), point, stats["radius"], stats["height"]
                ):
                    continue
                score = math.dist((x, y), escape) - math.dist(point, escape)
                if enemies:
                    threat = enemies[0]["pos"][:2]
                    score += 0.7 * (
                        math.dist(point, threat) - math.dist((x, y), threat)
                    )
                score += 0.1 * min(30, margin)
                if best is None or score > best[0]:
                    best = score, point
            if best and best[0] > 0:
                return best[1]
        return best[1] if best else escape

    def find_cover(self, obs, enemies):
        me, zone, t = obs["self"], obs["zone"], obs["t"]
        x, y, z = me["pos"]
        if t < self.cover_refresh:
            return self.cover
        self.cover_refresh = t + 0.55
        self.recovery_exit = None
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
        heal_wait = max(0, me["stats"]["regen_delay"] - (t - self.last_combat))
        recovery_horizon = min(6, heal_wait + 1.6)
        if (
            visible_now == 0
            and self.retreat_zone_margin(obs, (x, y), recovery_horizon) > 0
        ):
            self.cover = (x, y)
            self.recovery_exit = self.cover_exit(
                obs, (x, y, z), enemies, recovery_horizon
            )
            return self.cover
        best = None
        deadline = perf_counter() + 0.006
        away = math.atan2(y - enemies[0][1], x - enemies[0][0])
        stats = me["stats"]
        margin = max(3.0, min(10.0, zone["dps"] * 0.075))
        for radius in (12, 25, 39):
            for offset in (0.0, -0.7, 0.7, -1.4, 1.4, -2.1, 2.1):
                if perf_counter() >= deadline:
                    break
                px = x + radius * math.cos(away + offset)
                py = y + radius * math.sin(away + offset)
                eta = radius / stats["walk_speed"] + 0.5
                if self.retreat_zone_margin(obs, (px, py), eta) < margin:
                    continue
                pz = self.arena.ground(px, py)
                if not self.arena.can_stand(
                    px, py, pz, me["stats"]["radius"], me["stats"]["height"]
                ):
                    continue
                exposed = sum(
                    self.arena.clear(enemy, (px, py, pz + torso)) for enemy in enemies
                )
                if exposed >= max(1, visible_now):
                    continue
                if me["grounded"] and not self.arena.walk_clear(
                    (x, y, z), (px, py), stats["radius"], stats["height"]
                ):
                    continue
                progress = math.dist((px, py), zone["next_center"]) - math.dist(
                    (x, y), zone["next_center"]
                )
                score = 24 * exposed + 0.45 * radius + 0.16 * max(0, progress)
                if (
                    self.retreat_zone_margin(obs, (px, py), max(eta, recovery_horizon))
                    < margin
                ):
                    score += 12
                if best is None or score < best[0]:
                    backup = self.cover_exit(
                        obs, (px, py, pz), enemies, max(eta, recovery_horizon)
                    )
                    score += 1 if backup is None else 0
                    if best is None or score < best[0]:
                        best = (score, (px, py), backup)
            if best and best[0] < 14:
                break
        self.cover = best[1] if best else None
        self.recovery_exit = best[2] if best else None
        return self.cover

    def cover_exit(self, obs, point, eyes, eta):
        stats = obs["self"]["stats"]
        cx, cy = obs["zone"]["next_center"]
        direction = math.atan2(cy - point[1], cx - point[0])
        from_me = unit(
            point[0] - obs["self"]["pos"][0], point[1] - obs["self"]["pos"][1]
        )
        for offset in (0, 0.8, -0.8):
            ux, uy = math.cos(direction + offset), math.sin(direction + offset)
            if ux * from_me[0] + uy * from_me[1] < -0.65:
                continue
            x, y = point[0] + 8 * ux, point[1] + 8 * uy
            if self.retreat_zone_margin(obs, (x, y), eta + 1) < 4:
                continue
            if self.arena.walk_clear(point, (x, y), stats["radius"], stats["height"]):
                z = self.arena.ground(x, y)
                if all(
                    not self.arena.clear(eye, (x, y, z + stats["height"] * 0.58))
                    for eye in eyes
                ):
                    return x, y
        return None

    def use_recovery_exit(self, obs, threats):
        me, t = obs["self"], obs["t"]
        if (
            not self.cover
            or not self.recovery_exit
            or not threats
            or t - self.last_damage > 0.35
        ):
            return None
        pos, stats = me["pos"], me["stats"]
        backup = self.recovery_exit
        if (
            math.dist(pos[:2], self.cover) > 3
            or self.retreat_zone_margin(obs, backup, 2.4) < 5
        ):
            return None
        eyes = [(*e["pos"][:2], e["pos"][2] + 7) for e in threats[:3]]
        height = stats["height"] * 0.58
        now_exposed = any(
            self.arena.clear(eye, (*pos[:2], pos[2] + height)) for eye in eyes
        )
        later_exposed = any(
            self.arena.clear(eye, (*backup, self.arena.ground(*backup) + height))
            for eye in eyes
        )
        if (
            now_exposed
            and not later_exposed
            and self.arena.walk_clear(pos, backup, stats["radius"], stats["height"])
        ):
            self.cover, self.recovery_exit = backup, None
            self.cover_refresh = t + 0.55
            return backup
        return None

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

    def choose_shot(self, target, me, muzzle, retreat, alive, t, urgent):
        weapons = me["weapons"]
        if not target:
            charged = [
                bool(
                    w
                    and w["kind"] == "laser"
                    and w.get("charge", 0) > 0
                    and (
                        w["charge"] < 0.1
                        or (
                            not retreat
                            and not urgent
                            and t - self.last_laser_target_time <= 0.25
                        )
                    )
                )
                for w in weapons
            ]
            return (self.last_laser_aim or me["aim"], charged) if any(charged) else None
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
            if kind_w == "laser" and t < self.laser_blocked_until:
                continue
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
            if kind_w == "lightning":
                pitch = clamp(pitch, limits["min_pitch"], limits["max_pitch"])
            elif not limits["min_pitch"] <= pitch <= limits["max_pitch"]:
                continue
            yaw_error = abs(angle_delta(yaw, current[0]))
            pitch_error = abs(pitch - current[1])
            aim_error = (
                yaw_error if kind_w == "lightning" else max(yaw_error, pitch_error)
            )
            slew = self.sim_step * (
                0.75
                if weapon["cooldown"] <= self.sim_step or kind_w == "laser"
                else 1.65
            )
            aligned = yaw_error < tolerance + limits["turn_rate"] * slew and (
                kind_w == "lightning"
                or pitch_error < tolerance + limits["pitch_rate"] * slew
            )
            ready = weapon["cooldown"] <= max(0.0, self.dt - 0.015)
            fire = ready and aligned
            score = {"gun": 1.8, "lightning": 2.75, "laser": 2.65, "mortar": 2.2}[
                kind_w
            ]
            if (
                kind_w == "gun"
                and fire
                and target["id"] == self.short_target
                and t < self.short_until
            ):
                score += 1.25
            if kind_w == "laser":
                self.last_laser_aim = (yaw, pitch)
                self.last_laser_target_time = t
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
        selected = max(candidates)
        _, primary, _, _, trigger, _ = selected
        selected_weapon = weapons[primary]
        maintaining_laser = (
            selected_weapon["kind"] == "laser" and selected_weapon.get("charge", 0) > 0
        )
        if not trigger and not maintaining_laser:
            ready_gun = next(
                (
                    shot
                    for shot in candidates
                    if shot[4] and weapons[shot[1]]["kind"] == "gun"
                ),
                None,
            )
            if ready_gun is not None:
                selected = ready_gun
        _, index, yaw, pitch, trigger, _ = selected
        fire = [False, False]
        fire[index] = trigger
        for _, other, other_yaw, other_pitch, wants_fire, tol in candidates:
            if other != index and weapons[other]["kind"] != "laser":
                yaw_matches = abs(angle_delta(yaw, other_yaw)) < tol
                pitch_matches = abs(pitch - other_pitch) < tol
                fire[other] = (
                    wants_fire
                    and yaw_matches
                    and (pitch_matches or weapons[other]["kind"] == "lightning")
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
        if (
            self.damage_bearing is not None
            and t - self.last_damage < 0.6
            and abs(angle_delta(self.damage_bearing, obs["self"]["aim"][0])) > 1.35
        ):
            return self.damage_bearing, 0.0
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
