"""Teste de ponta a ponta: dois ramais registrados num PABX falso local.

Uso: python3 tests/test_calls.py
"""
import os, sys, tempfile, time, logging
_here = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(_here), _here]
import sipcore
from fake_pbx import FakePBX
logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")

class UI:
    def __init__(s, n): s.n = n; s.reg = None; s.incoming = []; s.states = []; s.msgs = []
    def on_reg_state(s, st, t): s.reg = st; print(s.n, "REG", st, t)
    def on_incoming(s, c): s.incoming.append(c); print(s.n, "INCOMING from", c.number, c.display)
    def on_call_update(s, c): s.states.append(c.state); print(s.n, "CALL", c.number, c.state, c.reason)
    def on_transfer_result(s, *a): print(s.n, "XFER", a)
    def on_message(s, d, u, t): s.msgs.append(t); print(s.n, "MSG", d, u, t)
    def on_message_status(s, *a): print(s.n, "MSGSTATUS", a)

pbx = FakePBX()
def acc(u, d): return dict(user=u, domain="127.0.0.1", server="127.0.0.1:15060", login=u, password="senha"+u, display=d, expires=300, keepalive=15, allow_rewrite=True)
cfg = dict(sip_port=0, codecs=["PCMA","PCMU"], dtmf="auto")
ua, ub = UI("A"), UI("B")
a, b = sipcore.SipEngine(ua), sipcore.SipEngine(ub)
a.hub.mic_mute = b.hub.mic_mute = True
a.hub.spk_mute = b.hub.spk_mute = True
a.start(acc("3003","ZECA"), cfg); b.start(acc("3004","KARINA"), cfg)
time.sleep(1); assert ua.reg == ub.reg == "online", (ua.reg, ub.reg)
call = a.make_call("3004"); time.sleep(1)
assert ub.incoming, "B não recebeu"
inc = ub.incoming[0]; assert inc.display == "ZECA"
b.answer(inc); time.sleep(1.5)
assert call.state == "confirmed" and inc.state == "confirmed", (call.state, inc.state)
time.sleep(1)
print("RTP A jitter:", len(call.rtp.jitter), "B:", len(inc.rtp.jitter), "A seq", call.rtp.last_seq, "B seq", inc.rtp.last_seq)
assert call.rtp.last_seq is not None and inc.rtp.last_seq is not None, "sem RTP"
a.send_dtmf(call, "5"); time.sleep(0.5)
a.set_hold(call, True); time.sleep(0.8); print("A hold:", call.state, "B remote_hold:", inc.remote_hold); assert inc.remote_hold
a.set_hold(call, False); time.sleep(0.8); assert not inc.remote_hold and call.state == "confirmed"
a.set_recording(call, os.path.join(tempfile.gettempdir(), "micruxsip-teste.wav")); time.sleep(1); a.set_recording(call, None)
b.hangup(inc); time.sleep(0.8)
assert call.state == "ended", call.state
a.send_message("3004", "olá teste"); time.sleep(0.5); assert ub.msgs == ["olá teste"]
# chamada cancelada
c2 = a.make_call("3004"); time.sleep(0.6); a.hangup(c2); time.sleep(0.8)
assert ub.incoming[-1].state == "ended", ub.incoming[-1].state
# recusada
c3 = a.make_call("3004"); time.sleep(0.6); b.hangup(ub.incoming[-1]); time.sleep(0.8)
print("recusada:", c3.reason); assert c3.reason.startswith("603")
# inexistente
c4 = a.make_call("9999"); time.sleep(0.8); print("inexistente:", c4.reason); assert c4.reason.startswith("404")
a.stop(); b.stop()
print("PBX viu:", pbx.events)
print("TUDO OK")
