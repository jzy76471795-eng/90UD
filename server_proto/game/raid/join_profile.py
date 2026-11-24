"""Selected-account data for the native raid application display.

The u16 exorcism value is the native 011991E0 -> 015BB170 display hint.
It is relayed as a client report, never used to authorize membership, entry,
combat, ticket costs or rewards. Those continue to use server-owned state.
Wallet, remaining fatigue and recovery percentage come from existing server
producers; the client's u32 field cannot substitute for any of them.
"""
from game.character.fatigue_flow import login_balance
from .ingress_protocol import RaidJoinRequest
from .native_join import JoinProfile
from .runtime import RaidError


class SelectedJoinProfileResolver:
    def __init__(self, service):
        self.service = service

    def __call__(self, session, request):
        service, server = self.service, self.service.server
        cid, account, _record = service._selected_actor(session)
        if (server.account_id != account or type(request) is not RaidJoinRequest
                or session.account_id != account or session.selected_character_id != cid):
            raise RaidError('native application profile requires its authenticated account thread')
        actor = service.native_lobby._own(session)
        proof = service.native_lobby.self_profile.proof(session)
        if (actor.character_id != cid or actor.account_id != account
                or proof.actor is not actor or type(proof.value) is not int or not 0 <= proof.value <= 100):
            raise RaidError('native application recovery profile is not current')
        repo = server._repo()
        ok, _note, wallet = repo.get_account_wallet(cid)
        if not ok or type(wallet.get('revive_coin')) is not int or wallet['revive_coin'] < 0:
            raise RaidError('native application account wallet unavailable')
        balance = login_balance(repo, cid)
        return JoinProfile(wallet['revive_coin'], balance.remaining, proof.value, request.derived_value_u16)
