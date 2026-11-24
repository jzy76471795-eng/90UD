"""手动下发 S0/0x0025 EXP 通知（调试用，文件触发）。

触发文件::

  server_proto/data/force_exp_notify.json

示例::

  {"level": 90, "exp": 1667387095, "expertise_exp": 18668021}
  {"level": 2, "exp": 1200, "gained": 56}   # exp 可省略：用 DB 累计 + gained

每次请求只发送一个原生 codec9 ``S0/0025``；客户端在入队前解密后交给 reader。

处理成功后改名为 ``force_exp_notify.done.json``。
客户端假死风险：本模块只在已有 Game 连接上 sendall，不挂断点。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from game.chain_loader import load_chain

_payloads = load_chain("helper-dungeon_payloads.py")
build_exp_notification_0025 = _payloads.build_exp_notification_0025

# game/character/this.py -> parents[2] = server_proto
DATA_DIR = Path(__file__).resolve().parents[2] / "data"
REQUEST_PATH = DATA_DIR / "force_exp_notify.json"
DONE_PATH = DATA_DIR / "force_exp_notify.done.json"


class ForceExpNotifyService:
    """轮询请求文件，向所有登记的 live Game 连接发 S0/0025。"""

    def __init__(self, server: Any) -> None:
        self.server = server
        self._lock = threading.Lock()
        self._clients: list[tuple[Any, Any]] = []  # (socket, session)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="force-exp-notify",
            daemon=True,
        )
        self._thread.start()
        try:
            self.server.log(
                f"[ForceExp] watcher on {REQUEST_PATH} (write JSON to push S0/0025)"
            )
        except Exception:
            pass

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=1.0)
        self._thread = None

    def register_client(self, client: Any, session: Any) -> None:
        with self._lock:
            self._clients = [(c, s) for c, s in self._clients if c is not client]
            self._clients.append((client, session))

    def unregister_client(self, client: Any) -> None:
        with self._lock:
            self._clients = [(c, s) for c, s in self._clients if c is not client]

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if REQUEST_PATH.is_file():
                    self._handle_request_file()
            except Exception as exc:
                try:
                    self.server.log(f"[ForceExp] loop err={exc!r}")
                except Exception:
                    pass
            self._stop.wait(0.15)

    def _build_pkt(
        self,
        level: int,
        exp: int,
        expertise_exp: int,
        layout: str,
    ) -> tuple[bytes, bytes, str]:
        body = build_exp_notification_0025(
            level,
            exp,
            expertise_exp=expertise_exp,
            layout=layout,
        )
        pkt = self.server._codec_response(0, 0x0025, body)
        return body, pkt, "codec9"

    def _send_all(
        self,
        pkt: bytes,
        level: int,
        exp: int,
        expertise_exp: int,
    ) -> tuple[int, list[Any]]:
        with self._lock:
            clients = list(self._clients)
        sent = 0
        dead: list[Any] = []
        for client, session in clients:
            try:
                client.sendall(pkt)
                sent += 1
                if session is not None:
                    try:
                        session.character_level = level
                        session.character_exp = exp
                        session.character_expertise_exp = expertise_exp
                        session.client_exp_primed = True
                    except Exception:
                        pass
            except OSError as exc:
                self.server.log(f"[ForceExp] send failed (drop stale): {exc!r}")
                dead.append(client)
        if dead:
            with self._lock:
                self._clients = [(c, s) for c, s in self._clients if c not in dead]
        return sent, dead

    def _handle_request_file(self) -> None:
        try:
            raw = REQUEST_PATH.read_text(encoding="utf-8")
            req = json.loads(raw) if raw.strip() else {}
        except Exception as exc:
            self.server.log(f"[ForceExp] bad request file: {exc!r}")
            self._archive_request({"error": repr(exc)})
            return

        level, exp, expertise_exp, note = self._resolve_level_exp(req)
        layout = str(
            req.get("layout")
            or os.environ.get("USDOF_GAME_DUNGEON_EXP_NOTI_LAYOUT")
            or "stage3_78"
        )

        with self._lock:
            clients = list(self._clients)
        if not clients:
            self.server.log(
                f"[ForceExp] no live game client; keep request level={level} "
                f"exp={exp}"
            )
            return

        body, pkt, wire_note = self._build_pkt(level, exp, expertise_exp, layout)
        sent, _ = self._send_all(pkt, level, exp, expertise_exp)

        wire_prefix = pkt[: min(40, len(pkt))].hex(" ")
        self.server.log(
            f"[ForceExp] S0/0025 sent={sent} level={level} exp={exp} "
            f"expertise_exp={expertise_exp} "
            f"layout={layout} wire={wire_note} "
            f"body_len={len(body)} pkt_len={len(pkt)} note={note} "
            f"pkt_prefix={wire_prefix}"
        )
        self._archive_request(
            {
                "level": level,
                "exp": exp,
                "expertise_exp": expertise_exp,
                "layout": layout,
                "wire": wire_note,
                "sent": sent,
                "note": note,
                "body_hex": body.hex(),
                "pkt_len": len(pkt),
                "pkt_prefix_hex": wire_prefix,
            }
        )

    def _resolve_level_exp(
        self,
        req: dict[str, Any],
    ) -> tuple[int, int, int, str]:
        """返回 ``(level, final_exp, expertise_exp, note)``。"""
        level = int(req.get("level") or 0)
        exp = req.get("exp")
        expertise_exp = req.get("expertise_exp")
        gained = int(req.get("gained") or 0)
        cid = int(req.get("character_id") or 0)
        note = "explicit"
        db_exp: int | None = None
        db_lv: int | None = None
        db_expertise_exp: int | None = None

        try:
            repo = self.server._repo()
            if cid <= 0:
                with self._lock:
                    for _c, session in self._clients:
                        for attr in ("selected_character_id", "character_id", "cid"):
                            val = getattr(session, attr, None)
                            if val is not None and int(val) > 0:
                                cid = int(val)
                                break
                        if cid > 0:
                            break
            if cid > 0:
                lv0, exp0, expertise0 = repo.get_character_progression(int(cid))
                db_exp = int(exp0 or 0) & 0xFFFFFFFF
                db_lv = max(1, int(lv0 or 1))
                db_expertise_exp = max(0, int(expertise0 or 0))
                if level <= 0:
                    level = db_lv
                if exp is None:
                    exp = db_exp + max(0, gained)
                    note = (
                        f"db_cid={cid}+gained={gained}"
                        if gained > 0
                        else f"db_cid={cid}"
                    )
                elif gained > 0 and not req.get("exp_is_total", True):
                    exp = int(exp) + gained
                    note = f"exp+gained={gained}"
                if expertise_exp is None:
                    expertise_exp = db_expertise_exp
        except Exception as exc:
            note = f"db_err={exc!r}"

        if exp is None:
            exp = 0
        if level <= 0:
            level = 1
        if expertise_exp is None:
            expertise_exp = 0

        final_exp = int(exp) & 0xFFFFFFFF

        return (
            max(1, min(255, int(level))),
            final_exp,
            max(0, int(expertise_exp)),
            note,
        )

    def _archive_request(self, meta: dict[str, Any]) -> None:
        try:
            if REQUEST_PATH.is_file():
                REQUEST_PATH.unlink()
        except Exception:
            pass
        try:
            DONE_PATH.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass
