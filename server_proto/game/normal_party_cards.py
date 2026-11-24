"""Owner-thread settlement publication and ordinary-party card presentation.

Each account freezes/grants its own rewards through the existing handlers.
Only immutable UI bodies cross to another account's Game pump. No inventory
update, wallet packet, repository call or encoder runs under party/run locks.
"""
from dataclasses import dataclass, field
import os
import struct

from game.chain_loader import load_chain
from game.game_wire import parse_game_header
from game.raid.runtime import RaidError


def _enabled(name, default=True):
    return str(os.environ.get(name, '1' if default else '0')).strip().lower() not in {'0','false','off','no',''}


def _free_groups(body):
    """Read the ordinary builder's bounded S0/0023 card section."""
    raw = bytes(body)
    if len(raw) < 182 or raw[101:103] != b'\0\0' or raw[123:128] != b'\0'*5:
        raise RaidError('ordinary clear reward layout changed')
    groups, offset = [], 128
    for _ in range(8):
        if offset >= len(raw):
            raise RaidError('truncated ordinary free-card groups')
        count = raw[offset]; offset += 1
        end = offset + count * 8
        if end > len(raw):
            raise RaidError('truncated ordinary free-card rewards')
        groups.append(tuple(struct.unpack_from('<II', raw, i) for i in range(offset,end,8)))
        offset = end
    if len(raw) != offset + 46:
        raise RaidError('unexpected ordinary clear reward tail')
    return tuple(groups), offset


def _shared_body(body, groups):
    _, end = _free_groups(body)
    card = bytearray()
    for slot in range(8):
        pairs = groups.get(slot, ())
        card.append(len(pairs))
        for key, value in pairs:
            card.extend(struct.pack('<II',key,value))
    return body[:128] + bytes(card) + body[end:]


@dataclass(frozen=True)
class Result:
    member: object
    generation: int
    packets: tuple
    clear_body: bytes
    pairs: tuple


@dataclass
class Board:
    run: object
    results: dict = field(default_factory=dict)
    sent: set = field(default_factory=set)
    notices: dict = field(default_factory=dict)


class PartyCards:
    def __init__(self, parties):
        self.parties, self.boards = parties, {}

    def _scope(self, session):
        """Caller holds runs._metadata; return an exact live run member."""
        runs = self.parties.runs
        run = runs._current(session)
        if run is None or not run.active or len(run.participants) < 2 or not runs._valid(run):
            return None
        member = runs._member(run, session)
        if (not runs._scene(run, member) or not 0 <= member.slot < 4
                or self.parties.server.account_id != member.account_id):
            return None
        if (getattr(session,'altar_final_clear_pending',False)
                or getattr(session,'dungeon_tournament_completed',False)
                or int(getattr(session,'cur_dungeon_id',0) or 0) in (120,121,122)):
            return None
        return run, member

    def _members(self, board):
        runs = self.parties.runs
        return tuple(m for m in runs._members(board.run) if runs._scene(board.run,m))

    def _ready(self, board, members):
        return bool(members) and all(
            (row := board.results.get(m.selection[0])) is not None
            and row.member is m
            and row.generation == getattr(m.session,'dungeon_run_generation',None)
            and int(getattr(m.session,'dungeon_clear_phase',0) or 0) >= 2
            for m in members)

    def stage_result(self, session, packets, clear_body):
        """Hold result UI until every surviving owner has frozen its plan."""
        if clear_body is None or not _enabled('USDOF_GAME_DUNGEON_CARD_AUTO_FLIP') or _enabled('USDOF_GAME_DUNGEON_SKIP_CARD_FLIP',False):
            return packets, ''
        groups, _ = _free_groups(clear_body)
        with self.parties.runs._metadata():
            scope = self._scope(session)
            if scope is None:
                return packets, ''
            run, member = scope
            board = self.boards.setdefault(id(run),Board(run))
            connection = member.selection[0]
            if connection not in board.results:
                board.results[connection] = Result(member,
                    int(session.dungeon_run_generation),tuple(packets),bytes(clear_body),groups[0])
        return [], 'ordinary shared result staged on owner pump'

    def route(self, opcode, body, session):
        with self.parties.runs._metadata():
            scope = self._scope(session)
            if scope is None:
                return None
            run, member = scope
            board = self.boards.get(id(run))
            if (board is None or member.selection[0] not in board.sent
                    or not self._ready(board,self._members(board))):
                raise RaidError('ordinary cards await every owner result')
        raw = bytes(body or b'')
        if opcode == 0x45:
            if raw:
                raise RaidError('ordinary score timer must be bodyless')
            if (not _enabled('USDOF_GAME_DUNGEON_CARD_AUTO_FLIP')
                    or _enabled('USDOF_GAME_DUNGEON_SKIP_CARD_FLIP',False)
                    or not getattr(session,'dungeon_card_auto_flip_armed',False)
                    or getattr(session,'dungeon_card_auto_flip_run_generation',None) != session.dungeon_run_generation
                    or getattr(session,'dungeon_card_flipped',False)
                    or getattr(session,'dungeon_card_reward_plan',{}).get('free_delivered',False)):
                return [], 'ordinary free-card timer inactive'
            session.dungeon_card_auto_flip_armed = False
            session.dungeon_card_auto_flip_deadline_monotonic = 0.0
            card_type = 0
        else:
            if (len(raw) not in (2,8,16) or raw[0] not in (0,1,2)
                    or raw[1] != member.slot or any(raw[2:])):
                raise RaidError('ordinary card owner does not match authenticated run slot')
            card_type = raw[0]
        # Never trust a request's owner to choose an account/repository. The
        # wire id is the immutable native slot of this selected connection.
        service = load_chain('opcode0047-select_card.py').SelectCard0047Service(self.parties.server)
        packets, note = service.handle(bytes((card_type,member.slot)),
            session=session,owner_seat=member.slot)
        if opcode == 0x45:
            session.dungeon_card_auto_flip_fired = bool(getattr(session,'dungeon_card_flipped',False))
        notice = service.presentation
        if notice is not None:
            with self.parties.runs._metadata():
                current = self._scope(session)
                if current is not None and current[0] is run and self.boards.get(id(run)) is board:
                    for peer in self._members(board):
                        if peer is not member:
                            rows = board.notices.setdefault(peer.selection[0],[])
                            if len(rows) >= 8:
                                raise RaidError('ordinary card presentation queue full')
                            rows.append(bytes(notice))
        return packets, 'ordinary card slot=%s source=%04X; %s' % (member.slot,opcode,note)

    def pump(self, owner, client):
        session = owner[0].session
        with self.parties.runs._metadata():
            scope = self._scope(session)
            if scope is None or owner[0].client is not client or scope[1].selection != owner:
                return
            run, member = scope
            board = self.boards.get(id(run))
            if board is None:
                return
            members = self._members(board)
            if not self._ready(board,members):
                return
            connection = owner[0]
            result = None
            if connection not in board.sent:
                result = board.results[connection]
                groups = {m.slot:board.results[m.selection[0]].pairs for m in members}
                board.sent.add(connection)
            notices = tuple(board.notices.pop(connection,()))
        # Both generation checking and all cross-member metadata work ended
        # before invoking this account's encoder or socket.
        if result is not None:
            shared = _shared_body(result.clear_body,groups)
            for packet in result.packets:
                header = parse_game_header(packet[:16])
                if (header.flag_or_magic & 255, header.opcode) == (0,0x23):
                    packet = self.parties.server._codec_response(0,0x23,shared)
                client.sendall(packet)
        for body in notices:
            client.sendall(self.parties.server._codec_response(1,0x47,body))

    def forget(self, session):
        # Invoked after runs.detach/forget, under the party's metadata lock.
        with self.parties.runs._metadata():
            for key, board in tuple(self.boards.items()):
                if not any(self.parties.runs._current(m.session) is board.run
                           for m in board.run.participants):
                    self.boards.pop(key,None)


def stage_result(server, session, packets, clear_body):
    parties = getattr(server,'normal_parties',None)
    cards = getattr(parties,'cards',None)
    return cards.stage_result(session,packets,clear_body) if cards is not None else (packets,'')
