"""Connection lifecycle and control transport regressions. No live game/DB access."""
import contextlib
import errno
import gc
import http.client
import io
import json
import threading
import unittest
import weakref
from types import MethodType, SimpleNamespace as NS
from unittest.mock import Mock, patch

from control.contracts import ControlApiError
from control.control_server import ControlServer, _ControlHttpServer
from game.character.force_exp_notify import ForceExpNotifyService
from game.game_server import GameServer


class Client:
    def __init__(self):self.closed=False
    def close(self):self.closed=True
    def fileno(self):return -1 if self.closed else 100
    def getsockname(self):return ('127.0.0.1',10075)


class ConnectionLifecycle(unittest.TestCase):
    def game(self, behavior):
        game=NS(account_id=1,account_m_id='audit',launcher_account_id=1,port=10075,
                log=Mock(),_delayed_response_context=threading.local(),
                _resolve_channel_id_for_port=lambda _port:23)
        game.force_exp_notify=ForceExpNotifyService(game)
        game._delayed_response_connection=MethodType(GameServer._delayed_response_connection,game)
        game._handle_client_connection=behavior
        return game

    def run_bound(self,game,client):
        GameServer._handle_client_bound(game,client,('127.0.0.1',43210),10075)

    def test_normal_exit_releases_closed_connection_and_session_without_exp_send(self):
        refs=[]
        def connected(client,_addr,_port,**kw):
            session=kw['session_state'];refs.append(weakref.ref(session))
            game.force_exp_notify.register_client(client,session);client.close()
        game=self.game(connected);self.run_bound(game,Client());gc.collect()
        self.assertEqual(game.force_exp_notify._clients,[])
        self.assertIsNone(refs[0]())

    def test_protocol_exception_releases_session_and_preserves_original_error(self):
        def connected(client,_addr,_port,**kw):
            game.force_exp_notify.register_client(client,kw['session_state'])
            raise RuntimeError('protocol failure')
        game=self.game(connected)
        with self.assertRaisesRegex(RuntimeError,'protocol failure'):self.run_bound(game,Client())
        self.assertEqual(game.force_exp_notify._clients,[])
        self.assertIsNone(getattr(game._delayed_response_context,'active',None))

    def test_repeated_reconnects_do_not_accumulate_notification_sessions(self):
        def connected(client,_addr,_port,**kw):
            game.force_exp_notify.register_client(client,kw['session_state']);client.close()
        game=self.game(connected)
        for _ in range(64):self.run_bound(game,Client())
        self.assertEqual(len(game.force_exp_notify._clients),0)

    def test_old_connection_cleanup_preserves_other_live_connection(self):
        other=Client();other_session=NS(selected_character_id=102)
        def connected(client,_addr,_port,**kw):
            game.force_exp_notify.register_client(client,kw['session_state'])
            game.force_exp_notify.register_client(other,other_session)
        game=self.game(connected);self.run_bound(game,Client())
        self.assertEqual(game.force_exp_notify._clients,[(other,other_session)])
        self.assertFalse(other.closed)

    def test_delayed_scope_exit_failure_still_cleans_notification_session(self):
        def connected(client,_addr,_port,**kw):game.force_exp_notify.register_client(client,kw['session_state'])
        game=self.game(connected)
        @contextlib.contextmanager
        def broken_scope(*_args):
            yield
            raise RuntimeError('scope exit failure')
        game._delayed_response_connection=broken_scope
        with self.assertRaisesRegex(RuntimeError,'scope exit failure'):self.run_bound(game,Client())
        self.assertEqual(game.force_exp_notify._clients,[])

    def policy_game(self,error):
        game=self.game(None)
        game.register_control_game_client=Mock(side_effect=error)
        game._handle_client_connection=MethodType(GameServer._handle_client_connection,game)
        return game

    def test_access_denied_returns_normally_and_never_registers_exp_client(self):
        game=self.policy_game(ControlApiError('ACCESS_DENIED','policy blocked',status=403))
        game.force_exp_notify.register_client=Mock(wraps=game.force_exp_notify.register_client)
        with patch.dict('os.environ',USDOF_PVF_BOOT_MODE='core'):
            self.run_bound(game,Client())
        game.force_exp_notify.register_client.assert_not_called()
        self.assertTrue(any('ACCESS_DENIED' in call.args[0] for call in game.log.call_args_list))
        self.assertEqual(game.force_exp_notify._clients,[])

    def test_unexpected_control_error_is_not_hidden_as_access_denial(self):
        game=self.policy_game(ControlApiError('OTHER_ERROR','unexpected',status=503))
        with patch.dict('os.environ',USDOF_PVF_BOOT_MODE='core'):
            with self.assertRaises(ControlApiError) as caught:self.run_bound(game,Client())
        self.assertEqual(caught.exception.code,'OTHER_ERROR')
        self.assertEqual(game.force_exp_notify._clients,[])

    def test_access_denied_with_wrong_status_is_not_swallowed(self):
        game=self.policy_game(ControlApiError('ACCESS_DENIED','unexpected',status=500))
        with patch.dict('os.environ',USDOF_PVF_BOOT_MODE='core'):
            with self.assertRaises(ControlApiError):self.run_bound(game,Client())
        self.assertEqual(game.force_exp_notify._clients,[])


class ControlReply(unittest.TestCase):
    def service(self):
        logs=[]
        service=ControlServer(NS(log=logs.append),token='audit-token')
        return service,logs

    def handler(self,stage=None,error=None):
        stream=io.BytesIO()
        handler=NS(command='POST',path='/api/v1/command',close_connection=False,
                   send_response=Mock(),send_header=Mock(),end_headers=Mock(),wfile=stream)
        if stage=='write':handler.wfile=NS(write=Mock(side_effect=error))
        elif stage:setattr(handler,stage,Mock(side_effect=error))
        return handler,stream

    def test_success_retains_http_status_headers_and_json(self):
        service,_=self.service();handler,stream=self.handler()
        service._write_json(handler,200,{'ok':True,'request_id':'abc'})
        self.assertEqual(json.loads(stream.getvalue()),{'ok':True,'request_id':'abc'})
        handler.send_response.assert_called_once_with(200)
        handler.send_header.assert_any_call('Content-Length',str(len(stream.getvalue())))
        self.assertFalse(handler.close_connection)

    def test_winerror_10053_during_body_is_closed_and_request_logged(self):
        service,logs=self.service();handler,_=self.handler('write',OSError(10053,'aborted'))
        service._write_json(handler,200,{'ok':True,'request_id':'req-53'})
        self.assertTrue(handler.close_connection)
        self.assertTrue(any('req-53' in line and '200' in line and '10053' in line for line in logs))

    def test_broken_pipe_during_status_is_handled(self):
        service,_=self.service();handler,_=self.handler('send_response',BrokenPipeError(errno.EPIPE,'closed'))
        service._write_json(handler,401,{'ok':False})
        self.assertTrue(handler.close_connection)
        handler.end_headers.assert_not_called()

    def test_reset_during_header_flush_is_handled(self):
        service,_=self.service();handler,_=self.handler('end_headers',ConnectionResetError(errno.ECONNRESET,'reset'))
        service._write_json(handler,200,{'ok':True})
        self.assertTrue(handler.close_connection)

    def test_windows_reset_and_shutdown_codes_are_handled(self):
        for code in (10054,10058):
            with self.subTest(code=code):
                service,_=self.service();handler,_=self.handler('write',OSError(code,'closed'))
                service._write_json(handler,503,{'ok':False})
                self.assertTrue(handler.close_connection)

    def test_non_disconnect_oserror_remains_visible(self):
        service,_=self.service();handler,_=self.handler('write',OSError(errno.ENOSPC,'disk full'))
        with self.assertRaises(OSError):service._write_json(handler,200,{'ok':True})

    def test_serialization_error_remains_visible(self):
        service,_=self.service();handler,_=self.handler()
        with self.assertRaises(TypeError):service._write_json(handler,200,{'bad':object()})
        handler.send_response.assert_not_called()

    def test_completed_post_is_not_reexecuted_when_reply_connection_aborts(self):
        service,logs=self.service()
        service._authorize=Mock();service._read_json=Mock(return_value={'request_id':'once','command':'test.command'})
        service._handle_command=Mock(return_value=(200,{'ok':True,'request_id':'once','result':{'applied':True}}))
        handler,_=self.handler('write',ConnectionAbortedError(10053,'aborted'))
        handler.server=NS(control_service=service)
        service._make_handler().do_POST(handler)
        service._handle_command.assert_called_once()
        self.assertTrue(any('command=test.command' in line and 'code=OK' in line for line in logs))
        self.assertTrue(any('once' in line and 'response' in line for line in logs))

    def test_real_http_worker_survives_lost_reply_and_serves_next_health_request(self):
        service,logs=self.service();calls=[];worker_errors=[]
        service.game_server.submit_control_command=lambda **kw:calls.append(kw) or {'applied':True}
        service.game_server.get_control_session_snapshot=lambda:{'session_epoch':1}
        parent=service._make_handler()
        class LostReply(parent):
            def do_POST(self):
                original=self.wfile
                self.wfile=NS(write=Mock(side_effect=ConnectionAbortedError(10053,'aborted')))
                try:super().do_POST()
                finally:self.wfile=original
        class Server(_ControlHttpServer):
            def handle_error(self,_request,_client_address):worker_errors.append('uncaught worker error')
        server=Server(('127.0.0.1',0),LostReply);server.control_service=service
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.01},daemon=True)
        thread.start()
        try:
            conn=http.client.HTTPConnection(*server.server_address,timeout=3)
            conn.request('POST','/api/v1/command',body=json.dumps({'request_id':'tcp-once','command':'test.command'}),headers={'Authorization':'Bearer audit-token','Content-Type':'application/json'})
            with self.assertRaises(http.client.RemoteDisconnected):conn.getresponse()
            conn.close()
            health=http.client.HTTPConnection(*server.server_address,timeout=3)
            health.request('GET','/api/v1/health');response=health.getresponse()
            self.assertEqual(response.status,200);self.assertTrue(json.loads(response.read())['ok']);health.close()
            self.assertEqual(len(calls),1)
            self.assertEqual(worker_errors,[])
            self.assertTrue(any('tcp-once' in line and 'response disconnected' in line for line in logs))
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)


if __name__=='__main__':unittest.main()
