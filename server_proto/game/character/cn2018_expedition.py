"""Account collection and expedition state for the supplied CN 2018 rules."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import time
from typing import Callable, Mapping

from game.character.adventure_group import EXPERTISE_CHARACTER_LEVEL, calculate_adventure_group_for_account
from game.character.character_stats import decode_grow_type
from pvf.cn_expedition_index import ALLOW_GROUP_C, EXPEDITION_HOURS, ExpeditionIndex


CN_TIME = timezone(timedelta(hours=8))
RESET_HOUR = 6
MAX_PARTY = 4
RewardKey = tuple[int, int, int, int]


class ExpeditionRejected(ValueError):
    """The player action failed validation. Its transaction has no effects."""


@dataclass(frozen=True)
class ExpeditionMember:
    character_id: int
    name: str
    job: int
    branch: int
    expertise_exp: int
    attributes: tuple[str, ...]


def _server_day(now: int):
    return (datetime.fromtimestamp(now, CN_TIME) - timedelta(hours=RESET_HOUR)).date()


def expedition_rotation(index: ExpeditionIndex, now: int) -> tuple[int, dict[int, tuple[str, ...]]]:
    key = _server_day(now).toordinal() // index.rotation_days
    used_global: set[str] = set()
    result = {}
    for area in index.areas.values():
        scopes = dict(area.uniqueness)
        selected = []
        for slot, group in enumerate(area.attribute_groups):
            if group == ALLOW_GROUP_C:
                selected.append(group)
                continue
            excluded = used_global if scopes.get(group) == "global" else set()
            if scopes.get(group) == "local":
                excluded.update(selected)
            candidates = tuple(attribute for attribute in index.groups[group] if attribute not in excluded)
            if not candidates:
                raise ValueError("empty CN expedition rotation")
            seed = hashlib.sha256(f"{key}:{area.area_id}:{slot}".encode("ascii")).digest()
            attribute = candidates[int.from_bytes(seed[:8], "big") % len(candidates)]
            selected.append(attribute)
            if scopes.get(group) == "global":
                used_global.add(attribute)
        result[area.area_id] = tuple(selected)
    return key, result


class CN2018Expedition:
    def __init__(
        self, repo, index: ExpeditionIndex, *,
        verified_rewards: Mapping[RewardKey, int] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.repo = repo
        self.index = index
        self.verified_rewards = dict(verified_rewards or {})
        self.clock = clock

    def _member(self, row) -> ExpeditionMember | None:
        branch, awakening = decode_grow_type(row["grow_type"])
        key = (int(row["job"]), branch, awakening)
        if row["level"] < EXPERTISE_CHARACTER_LEVEL or key not in self.index.collection_characters:
            return None
        attributes = self.index.characters.get(key, ())
        return ExpeditionMember(
            row["character_id"], row["name"], key[0], branch, row["expertise_exp"], attributes,
        )

    def set_representative(self, account_id: int, character_id: int) -> ExpeditionMember:
        with self.repo.inventory_transaction_scope() as conn:
            row = conn.execute(
                "SELECT character_id, name, job, grow_type, level, expertise_exp FROM characters "
                "WHERE account_id=? AND character_id=? AND delete_flag=0",
                (account_id, character_id),
            ).fetchone()
            member = None if row is None else self._member(row)
            if member is None:
                raise ExpeditionRejected("collection_character_ineligible")
            # A changed advancement can move an existing representative to a new branch.
            conn.execute(
                "DELETE FROM account_cn2018_collection WHERE account_id=? AND character_id=?",
                (account_id, character_id),
            )
            conn.execute(
                "INSERT INTO account_cn2018_collection (account_id,job,branch,character_id) VALUES (?,?,?,?) "
                "ON CONFLICT (account_id,job,branch) DO UPDATE SET character_id=excluded.character_id",
                (account_id, member.job, member.branch, character_id),
            )
            return member

    def remove_representative(self, account_id: int, character_id: int) -> bool:
        with self.repo.inventory_transaction_scope() as conn:
            return conn.execute(
                "DELETE FROM account_cn2018_collection WHERE account_id=? AND character_id=?",
                (account_id, character_id),
            ).rowcount == 1

    def _members(self, conn, account_id: int, *, available_only: bool = False) -> tuple[ExpeditionMember, ...]:
        unavailable = set()
        if available_only:
            unavailable = {row[0] for row in conn.execute(
                "SELECT m.character_id FROM account_cn2018_expedition_member m "
                "JOIN account_cn2018_expedition r ON r.run_id=m.run_id "
                "WHERE r.account_id=? AND r.status='active'", (account_id,),
            )}
        rows = conn.execute(
            "SELECT c.character_id,c.name,c.job,c.grow_type,c.level,c.expertise_exp "
            "FROM account_cn2018_collection p JOIN characters c "
            "ON c.character_id=p.character_id AND c.account_id=p.account_id "
            "AND c.job=p.job AND (c.grow_type & 15)=p.branch "
            "WHERE p.account_id=? AND c.delete_flag=0 ORDER BY c.expertise_exp DESC,c.character_id",
            (account_id,),
        )
        return tuple(member for row in rows if row["character_id"] not in unavailable
                     and (member := self._member(row)) is not None and (not available_only or member.attributes))

    def collection(self, account_id: int) -> tuple[ExpeditionMember, ...]:
        with self.repo.open() as conn:
            return self._members(conn, account_id)

    def collection_candidates(self, account_id: int, job: int, branch: int) -> tuple[ExpeditionMember, ...]:
        with self.repo.open() as conn:
            rows = conn.execute(
                "SELECT character_id,name,job,grow_type,level,expertise_exp FROM characters "
                "WHERE account_id=? AND job=? AND (grow_type & 15)=? AND delete_flag=0 "
                "ORDER BY expertise_exp DESC,character_id",
                (account_id, job, branch),
            )
            return tuple(member for row in rows if (member := self._member(row)) is not None)

    def collection_view(self, account_id: int) -> list[dict]:
        """Project eligible characters and their currently saved representatives."""
        from pvf.adventure_group_index import get_adventure_group_index

        ranks = get_adventure_group_index()
        with self.repo.open() as conn:
            rows = conn.execute(
                "SELECT c.character_id,c.name,c.job,c.grow_type,c.level,c.expertise_exp,"
                "p.character_id IS NOT NULL AS registered FROM characters c "
                "LEFT JOIN account_cn2018_collection p ON p.account_id=c.account_id "
                "AND p.character_id=c.character_id AND p.job=c.job AND p.branch=(c.grow_type & 15) "
                "WHERE c.account_id=? AND c.delete_flag=0 AND c.level>=? "
                "ORDER BY c.job,(c.grow_type & 15),c.expertise_exp DESC,c.character_id",
                (account_id, EXPERTISE_CHARACTER_LEVEL),
            )
            return [
                {"character_id": member.character_id, "name": member.name, "job": member.job,
                 "branch": member.branch, "expertise_level": ranks.expertise_level_for_exp(member.expertise_exp),
                 "attribute_ids": [self.index.attributes[name] for name in member.attributes],
                 "registered": bool(row["registered"])}
                for row in rows if (member := self._member(row)) is not None
            ]

    def _area(self, account_id: int, area_id: int):
        area = self.index.areas.get(area_id)
        if area is None:
            raise ExpeditionRejected("expedition_area_unknown")
        if calculate_adventure_group_for_account(self.repo, account_id).adventure_level < area.required_level:
            raise ExpeditionRejected("expedition_area_locked")
        return area

    def _match_mask(self, member: ExpeditionMember, required: tuple[str, ...]) -> int:
        return sum(1 << i for i, rule in enumerate(required) if self.index.matches(member.attributes, rule))

    def expedition_candidates(self, account_id: int, area_id: int) -> tuple[
        int, tuple[str, ...], tuple[ExpeditionMember, ...]
    ]:
        self._area(account_id, area_id)
        key, rotation = expedition_rotation(self.index, int(self.clock()))
        required = rotation[area_id]
        with self.repo.open() as conn:
            candidates = self._members(conn, account_id, available_only=True)
        return key, required, tuple(member for member in candidates if self._match_mask(member, required))

    def auto_party(self, account_id: int, area_id: int) -> tuple[int, tuple[int, ...]]:
        key, required, candidates = self.expedition_candidates(account_id, area_id)
        # Keep the smallest team for each coverage mask. The roster order
        # breaks ties by expertise, then by character ID.
        teams: dict[int, tuple[int, ...]] = {0: ()}
        for member in candidates:
            match = self._match_mask(member, required)
            for covered, team in tuple(teams.items()):
                current = teams.get(covered | match)
                if len(team) < MAX_PARTY and (current is None or len(team) + 1 < len(current)):
                    teams[covered | match] = (*team, member.character_id)
        best = max(teams, key=lambda mask: (mask.bit_count(), -len(teams[mask])))
        return key, teams[best]

    @staticmethod
    def _run(conn, row, now: int) -> dict:
        result = dict(row)
        status = result["status"]
        if status == "active" and now >= result["finish_unix"]:
            result["status"] = "ready"
        result["required_attributes"] = tuple(json.loads(result["required_attributes"]))
        result["members"] = tuple(r[0] for r in conn.execute(
            "SELECT character_id FROM account_cn2018_expedition_member WHERE run_id=? ORDER BY slot",
            (result["run_id"],),
        ))
        return result

    def start(self, account_id: int, request_id: str, area_id: int, hours: int,
              members: tuple[int, ...], rotation_key: int) -> dict:
        members = tuple(members)
        if (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128
                or type(hours) is not int or hours not in EXPEDITION_HOURS
                or type(area_id) is not int or area_id <= 0 or type(rotation_key) is not int
                or not 1 <= len(members) <= MAX_PARTY or any(type(cid) is not int or cid <= 0 for cid in members)
                or len(set(members)) != len(members)):
            raise ExpeditionRejected("expedition_party_or_time_invalid")
        now = int(self.clock())
        with self.repo.inventory_transaction_scope() as conn:
            row = conn.execute(
                "SELECT * FROM account_cn2018_expedition WHERE account_id=? AND request_id=?", (account_id, request_id),
            ).fetchone()
            if row is not None:
                prior = self._run(conn, row, now)
                if (prior["area_id"], prior["finish_unix"] - prior["start_unix"], prior["members"], prior["rotation_key"]) != (
                    area_id, hours * 3600, members, rotation_key,
                ):
                    raise ExpeditionRejected("expedition_request_conflict")
                return prior
            area = self._area(account_id, area_id)
            key, rotation = expedition_rotation(self.index, now)
            if key != rotation_key:
                raise ExpeditionRejected("expedition_rotation_expired")
            if conn.execute(
                "SELECT 1 FROM account_cn2018_expedition WHERE account_id=? AND area_id=? AND status='active'",
                (account_id, area_id),
            ).fetchone() is not None:
                raise ExpeditionRejected("expedition_area_busy")
            available = {member.character_id: member for member in self._members(conn, account_id, available_only=True)}
            if any(cid not in available for cid in members):
                raise ExpeditionRejected("expedition_member_unavailable")
            required = rotation[area.area_id]
            coverage = 0
            for cid in members:
                match = self._match_mask(available[cid], required)
                if not match:
                    raise ExpeditionRejected("expedition_member_attribute_mismatch")
                coverage |= match
            matched = coverage.bit_count()
            reward = self.verified_rewards.get((area_id, hours, len(members), matched))
            if type(reward) is not int or not 0 < reward <= self.index.glory_cap:
                raise ExpeditionRejected("expedition_reward_unverified")
            run_id = conn.execute(
                "INSERT INTO account_cn2018_expedition "
                "(account_id,request_id,area_id,start_unix,finish_unix,rotation_key,required_attributes,matched_count,"
                "glory_reward,status) VALUES (?,?,?,?,?,?,?,?,?,'active')",
                (account_id, request_id, area_id, now, now + hours * 3600, key, json.dumps(required), matched, reward),
            ).lastrowid
            conn.executemany(
                "INSERT INTO account_cn2018_expedition_member (run_id,slot,character_id) VALUES (?,?,?)",
                [(run_id, slot, cid) for slot, cid in enumerate(members)],
            )
            return self._run(conn, conn.execute(
                "SELECT * FROM account_cn2018_expedition WHERE run_id=?", (run_id,),
            ).fetchone(), now)

    @staticmethod
    def _period(now: int) -> int:
        today = _server_day(now)
        return today.year * 100 + today.month

    @staticmethod
    def _balance(conn, account_id: int, period: int) -> int:
        row = conn.execute(
            "SELECT balance FROM account_cn2018_glory WHERE account_id=? AND period_yyyymm=?", (account_id, period),
        ).fetchone()
        return 0 if row is None else int(row["balance"])

    def status(self, account_id: int) -> dict:
        now = int(self.clock())
        key, rotation = expedition_rotation(self.index, now)
        adventure_level = calculate_adventure_group_for_account(self.repo, account_id).adventure_level
        with self.repo.open() as conn:
            runs = tuple(self._run(conn, row, now) for row in conn.execute(
                "SELECT * FROM account_cn2018_expedition WHERE account_id=? AND status='active' ORDER BY area_id",
                (account_id,),
            ))
            return {"server_time": now, "adventure_level": adventure_level,
                    "unlocked_areas": tuple(area.area_id for area in self.index.areas.values()
                                            if area.required_level <= adventure_level),
                    "rotation_key": key, "rotation": rotation, "runs": runs,
                    "glory_balance": self._balance(conn, account_id, self._period(now))}

    def _finish(self, account_id: int, run_id: int, *, claim: bool) -> dict:
        now = int(self.clock())
        with self.repo.inventory_transaction_scope() as conn:
            row = conn.execute(
                "SELECT * FROM account_cn2018_expedition WHERE account_id=? AND run_id=?", (account_id, run_id),
            ).fetchone()
            if row is None:
                raise ExpeditionRejected("expedition_run_unknown")
            run = self._run(conn, row, now)
            target = "claimed" if claim else "cancelled"
            if run["status"] == target:
                return run
            if run["status"] != ("ready" if claim else "active"):
                raise ExpeditionRejected("expedition_not_ready" if claim else "expedition_not_cancellable")
            credit = 0
            if claim:
                period = self._period(now)
                balance = self._balance(conn, account_id, period)
                credit = min(run["glory_reward"], max(0, self.index.glory_cap - balance))
                conn.execute(
                    "INSERT INTO account_cn2018_glory (account_id,period_yyyymm,balance) VALUES (?,?,?) "
                    "ON CONFLICT (account_id) DO UPDATE SET period_yyyymm=excluded.period_yyyymm,balance=excluded.balance",
                    (account_id, period, balance + credit),
                )
            conn.execute(
                "UPDATE account_cn2018_expedition SET status=?,glory_credited=? WHERE account_id=? AND run_id=?",
                (target, credit, account_id, run_id),
            )
            run.update(status=target, glory_credited=credit)
            return run

    def cancel(self, account_id: int, run_id: int) -> dict:
        return self._finish(account_id, run_id, claim=False)

    def claim(self, account_id: int, run_id: int) -> dict:
        return self._finish(account_id, run_id, claim=True)
