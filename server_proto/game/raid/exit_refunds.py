"""Retry exact persisted ticket returns independently of departed sessions.

The admission ledger remains the durable amount/owner authority. Runtime keys
freeze instance and owner before cleanup; restart recovery uses the same ledger.
"""
from time import monotonic


class ExitRefunds:
    def __init__(self, service):
        self.service = service
        self.pending = {}
        self.next_retry = 0

    def _log(self, exc):
        log = getattr(self.service.server, 'log', None)
        if callable(log):
            try:
                log('[Raid] departed member ticket return deferred: %s' % exc)
            except Exception:
                pass

    def depart(self, entry, character_id):
        with self.service._lock:
            adapter = self.service.battle_adapter
            battle = entry.domain.battle
            if (adapter is None or battle is None or battle.status == 'completed'
                    and (entry.domain.single or battle.phase == 1)):
                return
            member = entry.domain._admitted.get(character_id)
            if member is None:
                return
            key = (battle.instance_id, character_id)
            request = (adapter.ledger, int(member.account_id),
                       'timed_out' if battle.status == 'timed_out' else 'abandoned')
            # Save the exact instance before the live directory can be removed
            # or its numeric raid key reused by a different admission.
            self.pending.setdefault(key, request)
            try:
                adapter.refund_unfinished(entry, character_id)
            except Exception as exc:
                self.next_retry = monotonic() + 2
                self._log(exc)
            else:
                self.pending.pop(key, None)

    def retry(self, *, force=False):
        with self.service._lock:
            if not self.pending or not force and monotonic() < self.next_retry:
                return
            self.next_retry = monotonic() + 2
            try:
                now = self.service._recovery_clock()
            except Exception as exc:
                self.next_retry = monotonic() + 10
                self._log(exc)
                return
            from .ticket_refunds import queue
            for key, (ledger, account_id, reason) in tuple(self.pending.items()):
                try:
                    queue(ledger, key[0], key[1], account_id, reason, now)
                except Exception as exc:
                    self.next_retry = monotonic() + 10
                    self._log(exc)
                else:
                    self.pending.pop(key, None)
