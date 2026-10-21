"""PABX de teste: registrar com digest + proxy sem estado (UDP)."""
import hashlib, os, re, socket, sys, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sipcore import SipMessage, parse_addr, uri_user, parse_auth, gen_branch

PASS = {"3003": "senha3003", "3004": "senha3004"}
REALM = "fakepbx"

class FakePBX:
    def __init__(self, port=15060):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", port))
        self.port = port
        self.reg = {}
        self.events = []
        threading.Thread(target=self.loop, daemon=True).start()

    def reply(self, req, code, reason, extra=()):
        lines = [f"SIP/2.0 {code} {reason}"] + [f"Via: {v}" for v in req.get_all("via")]
        to = req.get("to")
        if ";tag=" not in to: to += ";tag=pbx1"
        lines += [f"From: {req.get('from')}", f"To: {to}", f"Call-ID: {req.call_id}", f"CSeq: {req.get('cseq')}", *extra, "Content-Length: 0"]
        self.sock.sendto(("\r\n".join(lines) + "\r\n\r\n").encode(), req.src)

    def check_auth(self, req, hdr):
        a = req.get(hdr)
        if not a: return False
        p = parse_auth(a); u = p["username"]
        H = lambda s: hashlib.md5(s.encode()).hexdigest()
        ha1 = H(f"{u}:{REALM}:{PASS.get(u,'')}"); ha2 = H(f"{req.method}:{p['uri']}")
        exp = H(f"{ha1}:{p['nonce']}:{p['nc']}:{p['cnonce']}:auth:{ha2}") if "qop" in p else H(f"{ha1}:{p['nonce']}:{ha2}")
        return exp == p["response"]

    def loop(self):
        while True:
            data, addr = self.sock.recvfrom(65535)
            if not data.strip(): continue
            m = SipMessage.parse(data, addr)
            raw = data
            if m.is_request:
                self.events.append(m.method)
                if m.method == "REGISTER":
                    if not self.check_auth(m, "authorization"):
                        self.reply(m, 401, "Unauthorized", [f'WWW-Authenticate: Digest realm="{REALM}", nonce="abc123", qop="auth", algorithm=MD5'])
                        continue
                    user = uri_user(parse_addr(m.get("to"))[1])
                    self.reg[user] = parse_addr(m.get_all("contact")[0])[1]
                    self.reply(m, 200, "OK", [f"Contact: <{self.reg[user]}>;expires=300"])
                    continue
                if m.method == "INVITE" and ";tag=" not in m.get("to") and not self.check_auth(m, "proxy-authorization"):
                    self.reply(m, 407, "Proxy Authentication Required", [f'Proxy-Authenticate: Digest realm="{REALM}", nonce="n2", qop="auth"'])
                    continue
                if m.method == "ACK" and ";tag=pbx1" in m.get("to"):
                    continue
                user = uri_user(m.uri)
                target = self.reg.get(user)
                if not target:
                    self.reply(m, 404, "Not Found"); continue
                host, port = re.match(r"sip:[^@]+@([^:;>]+):(\d+)", target).groups()
                via = f"Via: SIP/2.0/UDP 127.0.0.1:{self.port};branch={gen_branch()}\r\n"
                head, _, rest = raw.partition(b"\r\n")
                fixed = head + b"\r\n" + via.encode() + rest
                # Via recebido precisa de received/rport para a volta
                self.sock.sendto(fixed, (host, int(port)))
            else:
                vias = m.get_all("via")
                if len(vias) < 2: continue
                nxt = vias[1]
                rp = re.search(r"rport=(\d+)", nxt); rc = re.search(r"received=([\d.]+)", nxt)
                sb = re.match(r"SIP/2.0/UDP ([^:;]+):?(\d*)", nxt)
                dest = (rc.group(1) if rc else sb.group(1), int(rp.group(1)) if rp else int(sb.group(2) or 5060))
                # remove o primeiro Via (nosso)
                txt = raw.decode()
                idx = txt.lower().find("\r\nvia:")
                end = txt.find("\r\n", idx + 2)
                txt = txt[:idx] + txt[end:]
                self.sock.sendto(txt.encode(), dest)
