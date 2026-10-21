"""Pilha SIP/RTP do MicruxSip (UDP, G.711, DTMF RFC 2833).

Sem dependências externas: o áudio passa por parec/pacat (PulseAudio/PipeWire).
"""
import array
import hashlib
import logging
import os
import random
import re
import select
import socket
import struct
import subprocess
import threading
import time
import uuid
import wave
from collections import deque

log = logging.getLogger("sip")
USER_AGENT = "MicruxSip"
T1, T2 = 0.5, 4.0
FRAME = 160                    # 20 ms a 8 kHz
FRAME_BYTES = FRAME * 2
SILENCE = bytes(FRAME_BYTES)

# ---------------------------------------------------------------- G.711

def _ulaw_enc(s):
    sign = 0x80 if s < 0 else 0
    if s < 0:
        s = -s
    s = min(s, 32635) + 0x84
    exp = max(((s >> 7) & 0xFF).bit_length() - 1, 0)
    man = (s >> (exp + 3)) & 0x0F
    return ~(sign | (exp << 4) | man) & 0xFF


def _ulaw_dec(u):
    u = ~u & 0xFF
    exp, man = (u >> 4) & 7, u & 0x0F
    s = (((man << 3) + 0x84) << exp) - 0x84
    return -s if u & 0x80 else s


_SEG_AEND = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)


def _alaw_enc(s):
    v = s >> 3
    if v >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        v = -v - 1
    seg = next((i for i, e in enumerate(_SEG_AEND) if v <= e), 8)
    if seg >= 8:
        return 0x7F ^ mask
    a = seg << 4
    a |= (v >> 1) & 0x0F if seg < 2 else (v >> seg) & 0x0F
    return a ^ mask


def _alaw_dec(a):
    a ^= 0x55
    t = (a & 0x0F) << 4
    seg = (a & 0x70) >> 4
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t = (t + 0x108) << (seg - 1)
    return t if a & 0x80 else -t


class Codec:
    def __init__(self, name, pt, enc, dec):
        self.name, self.pt = name, pt
        self._enc_fn, self._dec_fn = enc, dec
        self._enc = None
        self._dec = [struct.pack("<h", dec(i)) for i in range(256)]

    def encode(self, pcm):
        if self._enc is None:
            self._enc = bytes(self._enc_fn(i - 65536 if i >= 32768 else i) for i in range(65536))
        return bytes(map(self._enc.__getitem__, array.array("H", pcm)))

    def decode(self, data):
        return b"".join(map(self._dec.__getitem__, data))


CODECS = {"PCMU": Codec("PCMU", 0, _ulaw_enc, _ulaw_dec),
          "PCMA": Codec("PCMA", 8, _alaw_enc, _alaw_dec)}
CODEC_BY_PT = {c.pt: c for c in CODECS.values()}


def warm_codecs():
    for c in CODECS.values():
        c.encode(SILENCE)


def pcm_gain(pcm, g):
    if g == 1.0:
        return pcm
    a = array.array("h", pcm)
    return array.array("h", (max(-32768, min(32767, int(x * g))) for x in a)).tobytes()


def pcm_mix(frames):
    frames = [f for f in frames if f]
    if not frames:
        return SILENCE
    if len(frames) == 1:
        return frames[0]
    arrs = [array.array("h", f) for f in frames]
    return array.array("h", (max(-32768, min(32767, sum(v))) for v in zip(*arrs))).tobytes()


# ---------------------------------------------------------------- mensagens SIP

COMPACT = {"v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact",
           "l": "content-length", "c": "content-type", "k": "supported", "o": "event",
           "r": "refer-to", "b": "referred-by", "s": "subject", "e": "content-encoding"}
MULTI = {"via", "route", "record-route", "contact"}


def split_commas(value):
    out, cur, depth, quote = [], "", 0, False
    for ch in value:
        if ch == '"':
            quote = not quote
        elif not quote and ch == "<":
            depth += 1
        elif not quote and ch == ">":
            depth -= 1
        if ch == "," and not quote and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


class SipMessage:
    def __init__(self):
        self.method = self.uri = self.reason = None
        self.status = 0
        self.headers = []          # (nome-minúsculo, valor)
        self.body = b""
        self.src = None

    @property
    def is_request(self):
        return self.method is not None

    def get(self, name, default=""):
        name = name.lower()
        for k, v in self.headers:
            if k == name:
                return v
        return default

    def get_all(self, name):
        name = name.lower()
        out = []
        for k, v in self.headers:
            if k == name:
                out.extend(split_commas(v) if name in MULTI else [v])
        return out

    @property
    def cseq(self):
        n, _, m = self.get("cseq").partition(" ")
        return int(n or 0), m.strip().upper()

    @property
    def call_id(self):
        return self.get("call-id")

    @property
    def top_branch(self):
        via = self.get_all("via")
        m = re.search(r";\s*branch=([^;\s,]+)", via[0]) if via else None
        return m.group(1) if m else ""

    @staticmethod
    def parse(data, src=None):
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.decode("utf-8", "replace").split("\r\n")
        if not lines or not lines[0]:
            return None
        msg = SipMessage()
        msg.src = src
        first = lines[0].split(" ", 2)
        if first[0].startswith("SIP/"):
            msg.status = int(first[1])
            msg.reason = first[2] if len(first) > 2 else ""
        else:
            if len(first) < 3:
                return None
            msg.method, msg.uri = first[0].upper(), first[1]
        for line in lines[1:]:
            if line[:1] in (" ", "\t") and msg.headers:
                k, v = msg.headers[-1]
                msg.headers[-1] = (k, v + " " + line.strip())
                continue
            k, _, v = line.partition(":")
            k = k.strip().lower()
            msg.headers.append((COMPACT.get(k, k), v.strip()))
        clen = msg.get("content-length")
        msg.body = rest[:int(clen)] if clen.isdigit() else rest
        return msg


def parse_addr(value):
    """'"Nome" <sip:u@h>;tag=x' -> (nome, uri, params)"""
    value = value.strip()
    display, params = "", {}
    m = re.match(r'^\s*(?:"((?:[^"\\]|\\.)*)"|([^<]*?))\s*<([^>]*)>(.*)$', value)
    if m:
        display = (m.group(1) if m.group(1) is not None else m.group(2) or "").strip()
        uri, tail = m.group(3), m.group(4)
    else:
        uri, _, tail = value.partition(";")
        tail = ";" + tail if tail else ""
    for p in tail.split(";"):
        if p.strip():
            k, _, v = p.strip().partition("=")
            params[k.lower()] = v
    return display, uri, params


def uri_user(uri):
    m = re.match(r"^(?:sips?|tel):([^@;>?]+)(?:@|$)", uri)
    return m.group(1) if m else uri


def uri_hostport(uri, default_port=5060):
    m = re.match(r"^sips?:(?:[^@]*@)?(\[[^\]]+\]|[^:;>?]+)(?::(\d+))?", uri)
    if not m:
        return None
    return m.group(1).strip("[]"), int(m.group(2) or default_port)


def gen_tag():
    return uuid.uuid4().hex[:10]


def gen_branch():
    return "z9hG4bK" + uuid.uuid4().hex[:16]


def parse_auth(value):
    _, _, rest = value.partition(" ")
    out = {}
    for m in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))', rest):
        out[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
    return out


def digest(chal, method, uri, user, pwd, nc=1):
    p = parse_auth(chal)
    alg = p.get("algorithm", "MD5")
    H = (lambda s: hashlib.sha256(s.encode()).hexdigest()) if alg.upper().startswith("SHA-256") \
        else (lambda s: hashlib.md5(s.encode()).hexdigest())
    realm, nonce = p.get("realm", ""), p.get("nonce", "")
    ha1 = H(f"{user}:{realm}:{pwd}")
    ha2 = H(f"{method}:{uri}")
    hdr = f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}"'
    qops = [q.strip() for q in p.get("qop", "").split(",")]
    if "auth" in qops:
        cnonce = uuid.uuid4().hex[:16]
        resp = H(f"{ha1}:{nonce}:{nc:08x}:{cnonce}:auth:{ha2}")
        hdr += f', response="{resp}", algorithm={alg}, cnonce="{cnonce}", qop=auth, nc={nc:08x}'
    else:
        hdr += f', response="{H(f"{ha1}:{nonce}:{ha2}")}", algorithm={alg}'
    if "opaque" in p:
        hdr += f', opaque="{p["opaque"]}"'
    return hdr


# ---------------------------------------------------------------- SDP

def build_sdp(ip, port, codecs, dtmf_pt, direction, sess_id, version):
    pts = [c.pt for c in codecs] + ([dtmf_pt] if dtmf_pt is not None else [])
    lines = ["v=0", f"o=- {sess_id} {version} IN IP4 {ip}", "s=MicruxSip", f"c=IN IP4 {ip}", "t=0 0",
             f"m=audio {port} RTP/AVP " + " ".join(map(str, pts))]
    for c in codecs:
        lines.append(f"a=rtpmap:{c.pt} {c.name}/8000")
    if dtmf_pt is not None:
        lines += [f"a=rtpmap:{dtmf_pt} telephone-event/8000", f"a=fmtp:{dtmf_pt} 0-16"]
    lines += ["a=ptime:20", f"a={direction}"]
    return ("\r\n".join(lines) + "\r\n").encode()


def parse_sdp(body):
    sdp = {"ip": None, "port": 0, "pts": [], "rtpmap": {}, "dtmf_pt": None, "direction": "sendrecv"}
    in_audio = False
    for line in body.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("c=") and (in_audio or sdp["ip"] is None):
            sdp["ip"] = line.split()[-1]
        elif line.startswith("m="):
            parts = line[2:].split()
            in_audio = parts[0] == "audio"
            if in_audio:
                sdp["port"] = int(parts[1])
                sdp["pts"] = [int(x) for x in parts[3:] if x.isdigit()]
        elif line.startswith("a=rtpmap:") and in_audio:
            pt, _, enc = line[9:].partition(" ")
            name = enc.split("/")[0]
            sdp["rtpmap"][int(pt)] = name.upper()
            if name.lower() == "telephone-event":
                sdp["dtmf_pt"] = int(pt)
        elif line[2:] in ("sendrecv", "sendonly", "recvonly", "inactive") and line.startswith("a="):
            if in_audio or sdp["direction"] == "sendrecv":
                sdp["direction"] = line[2:]
    return sdp


def sdp_codec(sdp, prefs):
    """Primeiro codec da oferta que nós suportamos (ordem do remoto)."""
    for pt in sdp["pts"]:
        name = sdp["rtpmap"].get(pt) or {0: "PCMU", 8: "PCMA"}.get(pt)
        if name in prefs:
            return CODECS[name]
    return None


# ---------------------------------------------------------------- áudio

def pa_sources():
    try:
        out = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True, timeout=3).stdout
        return [l.split("\t")[1] for l in out.splitlines() if "\t" in l]
    except Exception:
        return []


def pa_sinks():
    try:
        out = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True, timeout=3).stdout
        return [l.split("\t")[1] for l in out.splitlines() if "\t" in l]
    except Exception:
        return []


def default_mic():
    """O padrão do sistema às vezes é um .monitor (som do alto-falante); evita isso."""
    try:
        d = subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:
        d = ""
    if d and not d.endswith(".monitor"):
        return d
    real = [s for s in pa_sources() if not s.endswith(".monitor")]
    return real[0] if real else (d or None)


class AudioHub:
    """Relógio único de 20 ms: lê o microfone, mistura e distribui entre as chamadas."""

    def __init__(self):
        self.lock = threading.Lock()
        self.streams = []
        self.thread = None
        self.mic_dev = None
        self.spk_dev = None
        self.mic_gain = self.spk_gain = 1.0
        self.mic_mute = self.spk_mute = False
        self.conference = False

    def add(self, s):
        with self.lock:
            if s not in self.streams:
                self.streams.append(s)
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, daemon=True)
                self.thread.start()

    def remove(self, s):
        with self.lock:
            if s in self.streams:
                self.streams.remove(s)

    def _loop(self):
        mic = self.mic_dev or default_mic()
        base = ["--raw", "--format=s16le", "--rate=8000", "--channels=1"]
        rec = play = None
        try:
            rec = subprocess.Popen(["parec", *base, "--latency-msec=20"] + ([f"--device={mic}"] if mic else []),
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception as e:
            log.error("microfone indisponível: %s", e)
        try:
            play = subprocess.Popen(["pacat", "--playback", *base, "--latency-msec=60"] +
                                    ([f"--device={self.spk_dev}"] if self.spk_dev else []),
                                    stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception as e:
            log.error("alto-falante indisponível: %s", e)
        log.info("áudio iniciado (mic=%s, spk=%s)", mic, self.spk_dev or "padrão")
        next_t = time.monotonic()
        try:
            while True:
                with self.lock:
                    streams = list(self.streams)
                if not streams:
                    break
                frame = None
                if rec and rec.poll() is None:
                    frame = rec.stdout.read(FRAME_BYTES)
                    if len(frame) < FRAME_BYTES:
                        frame = None
                if frame is None:      # sem microfone: marca o tempo no relógio do sistema
                    next_t += 0.02
                    time.sleep(max(0, next_t - time.monotonic()))
                    frame = SILENCE
                mic_pcm = SILENCE if self.mic_mute else pcm_gain(frame, self.mic_gain)
                remotes = {s: s.pull() for s in streams}
                live = [s for s in streams if s.active()]
                speaker = pcm_mix([remotes[s] for s in live])
                if play and play.poll() is None:
                    try:
                        play.stdin.write(SILENCE if self.spk_mute else pcm_gain(speaker, self.spk_gain))
                        play.stdin.flush()
                    except Exception:
                        play = None
                for s in streams:
                    if not s.active():
                        out = SILENCE
                    elif self.conference and len(live) > 1:
                        out = pcm_mix([mic_pcm] + [remotes[o] for o in live if o is not s])
                    else:
                        out = mic_pcm
                    s.send(out)
                    if s.recorder:
                        s.recorder.write(pcm_mix([mic_pcm, remotes[s]]))
        except Exception:
            log.exception("erro no laço de áudio")
        finally:
            for p in (rec, play):
                if p:
                    try:
                        p.kill()
                    except Exception:
                        pass
            log.info("áudio parado")


class Recorder:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.w = wave.open(path, "wb")
        self.w.setnchannels(1)
        self.w.setsampwidth(2)
        self.w.setframerate(8000)
        self.lock = threading.Lock()

    def write(self, pcm):
        with self.lock:
            if self.w:
                self.w.writeframes(pcm)

    def close(self):
        with self.lock:
            if self.w:
                self.w.close()
                self.w = None


DTMF_EVENTS = {**{str(i): i for i in range(10)}, "*": 10, "#": 11, "A": 12, "B": 13, "C": 14, "D": 15}


class RtpStream:
    def __init__(self, port_min=4000, port_max=4999):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for _ in range(100):
            port = random.randrange(port_min, port_max) & ~1
            try:
                self.sock.bind(("0.0.0.0", port))
                break
            except OSError:
                continue
        else:
            self.sock.bind(("0.0.0.0", 0))
        self.port = self.sock.getsockname()[1]
        self.remote = None
        self.codec = CODECS["PCMA"]
        self.dtmf_pt = None
        self.seq = random.randrange(65536)
        self.ts = random.randrange(1 << 31)
        self.ssrc = random.randrange(1 << 32)
        self.marker = True
        self.hold = False          # espera local
        self.jitter = deque(maxlen=10)
        self.buffering = True
        self.last_seq = None
        self.dtmf_queue = deque()
        self.dtmf_cur = None
        self.recorder = None
        self.latched = False
        self.running = True
        threading.Thread(target=self._rx, daemon=True).start()

    def configure(self, ip, port, codec, dtmf_pt):
        self.remote = (ip, port)
        self.codec = codec
        self.dtmf_pt = dtmf_pt
        self.latched = False

    def active(self):
        return not self.hold and self.remote is not None

    def pull(self):
        if self.buffering:
            if len(self.jitter) < 3:
                return SILENCE
            self.buffering = False
        if not self.jitter:
            self.buffering = True
            return SILENCE
        return self.jitter.popleft()

    def _rx(self):
        while self.running:
            try:
                r, _, _ = select.select([self.sock], [], [], 0.5)
                if not r:
                    continue
                data, addr = self.sock.recvfrom(2048)
            except OSError:
                break
            if len(data) < 12 or data[0] >> 6 != 2:
                continue
            if not self.latched and self.remote:   # RTP simétrico (atravessa NAT)
                self.remote = addr
                self.latched = True
            cc = data[0] & 0x0F
            pt = data[1] & 0x7F
            seq = struct.unpack("!H", data[2:4])[0]
            off = 12 + 4 * cc
            if data[0] & 0x10 and len(data) >= off + 4:
                off += 4 + 4 * struct.unpack("!H", data[off + 2:off + 4])[0]
            payload = data[off:]
            if data[0] & 0x20 and payload:
                payload = payload[:-payload[-1]]
            codec = CODEC_BY_PT.get(pt)
            if not codec or self.hold:
                continue
            if self.last_seq is not None and ((seq - self.last_seq) & 0xFFFF) > 0x8000:
                continue   # pacote atrasado
            self.last_seq = seq
            pcm = codec.decode(payload)
            for i in range(0, len(pcm), FRAME_BYTES):
                chunk = pcm[i:i + FRAME_BYTES]
                self.jitter.append(chunk + bytes(FRAME_BYTES - len(chunk)))

    def _packet(self, pt, payload, marker=False, ts=None):
        hdr = struct.pack("!BBHII", 0x80, (0x80 if marker else 0) | pt, self.seq, self.ts if ts is None else ts, self.ssrc)
        self.seq = (self.seq + 1) & 0xFFFF
        try:
            self.sock.sendto(hdr + payload, self.remote)
        except OSError:
            pass

    def queue_dtmf(self, digit):
        if digit.upper() in DTMF_EVENTS:
            self.dtmf_queue.append(DTMF_EVENTS[digit.upper()])

    def send(self, pcm):
        if not self.remote:
            return
        if self.dtmf_cur is None and self.dtmf_queue and self.dtmf_pt is not None:
            self.dtmf_cur = [self.dtmf_queue.popleft(), 0, self.ts]
        if self.dtmf_cur:
            ev, n, ts0 = self.dtmf_cur
            n += 1
            dur = n * FRAME
            end = n >= 8
            pkt = struct.pack("!BBH", ev, (0x80 if end else 0) | 10, dur)
            for _ in range(3 if end else 1):
                self._packet(self.dtmf_pt, pkt, marker=(n == 1), ts=ts0)
            self.dtmf_cur[1] = n
            self.ts = (self.ts + FRAME) & 0xFFFFFFFF
            if end:
                self.dtmf_cur = None
                self.marker = True
            return
        self._packet(self.codec.pt, self.codec.encode(pcm), marker=self.marker)
        self.marker = False
        self.ts = (self.ts + FRAME) & 0xFFFFFFFF

    def close(self):
        self.running = False
        if self.recorder:
            self.recorder.close()
            self.recorder = None
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- transações e chamadas

class Req:
    """Uma requisição que pode ser reenviada com autenticação."""

    def __init__(self, method, ruri, from_, to, call_id, cseq, extra=None, body=b"", ctype=None,
                 routes=None, dest=None, contact=True):
        self.method, self.ruri, self.from_, self.to = method, ruri, from_, to
        self.call_id, self.cseq = call_id, cseq
        self.extra = list(extra or [])
        self.body, self.ctype = body, ctype
        self.routes = list(routes or [])
        self.dest = dest
        self.contact = contact
        self.auth = None
        self.auth_tries = 0
        self.branch = None


class ClientTx:
    def __init__(self, req, data, dest, cb):
        self.req, self.data, self.dest, self.cb = req, data, dest, cb
        self.invite = req.method == "INVITE"
        self.start = time.monotonic()
        self.interval = T1
        self.next = self.start + T1
        self.provisional = False
        self.final = False
        self.final_resp = None


class Call:
    _ids = 0

    def __init__(self, direction, number, display=""):
        Call._ids += 1
        self.id = Call._ids
        self.direction = direction
        self.number = number
        self.display = display
        self.state = "calling" if direction == "out" else "incoming"
        self.call_id = None
        self.local_tag = gen_tag()
        self.remote_tag = None
        self.local_addr = self.remote_addr = ""
        self.local_cseq = random.randrange(1, 10000)
        self.remote_target = None
        self.route_set = []
        self.invite_req = None
        self.invite = None          # INVITE recebido
        self.got_provisional = False
        self.cancel_pending = False
        self.rtp = None
        self.remote_sdp = None
        self.sdp_ver = 1
        self.sess_id = random.randrange(1, 1 << 30)
        self.hold = False
        self.remote_hold = False
        self.created = time.time()
        self.answered = None
        self.ended = None
        self.reason = ""
        self.uas_200 = None
        self.uas_200_next = 0
        self.uas_200_int = T1
        self.uas_200_start = 0
        self.ack_data = None
        self.ack_dest = None
        self.early_media = False
        self.transfer_to = None
        self.auto_answer = False

    @property
    def name(self):
        return self.display or self.number

    def next_cseq(self):
        self.local_cseq += 1
        return self.local_cseq


class SipEngine:
    def __init__(self, ui):
        """ui: objeto com on_reg_state, on_incoming, on_call_update, on_message, on_transfer_result
        (chamados a partir de threads; a interface reencaminha para o laço GTK)."""
        self.ui = ui
        self.lock = threading.RLock()
        self.hub = AudioHub()
        self.acc = None
        self.cfg = {}
        self.sock = None
        self.running = False
        self.ctx = {}
        self.stx = {}               # (branch, método) -> (resposta, destino, expira)
        self.calls = {}
        self.reg_state = "offline"
        self.reg_callid = None
        self.reg_cseq = 1
        self.reg_next = 0
        self.reg_expires = 300
        self.keep_next = 0
        self.public = None          # (ip, porta) visto pelo servidor
        self.local_ip = "127.0.0.1"

    # ---- ciclo de vida

    def start(self, acc, cfg):
        self.stop()
        self.acc, self.cfg = dict(acc), dict(cfg)
        self.public = None
        self.server = self._resolve(self.acc.get("proxy") or self.acc.get("server") or self.acc.get("domain"))
        self.local_ip = self._route_ip(self.server[0] if self.server else "8.8.8.8")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        port = int(self.cfg.get("sip_port") or 0)
        try:
            self.sock.bind(("0.0.0.0", port))
        except OSError:
            log.warning("porta %s ocupada, usando aleatória", port)
            self.sock.bind(("0.0.0.0", 0))
        self.port = self.sock.getsockname()[1]
        self.running = True
        self.reg_callid = uuid.uuid4().hex + "@" + self.local_ip
        self.reg_cseq = 1
        threading.Thread(target=self._rx_loop, daemon=True).start()
        threading.Thread(target=warm_codecs, daemon=True).start()
        threading.Thread(target=self._timer_loop, daemon=True).start()
        log.info("SIP em %s:%s -> servidor %s", self.local_ip, self.port, self.server)
        if self.server:
            self.register()
        else:
            self._set_reg("error", "Servidor inválido")

    def stop(self):
        if not self.running:
            return
        with self.lock:
            for c in list(self.calls.values()):
                self.hangup(c)
        if self.reg_state == "online":
            self.register(expires=0)
            time.sleep(0.3)
        self.running = False
        try:
            self.sock.close()
        except Exception:
            pass
        self.ctx.clear()
        self._set_reg("offline", "Offline")

    @staticmethod
    def _resolve(hostport):
        if not hostport:
            return None
        hostport = hostport.strip()
        if hostport.startswith("sip:"):
            hostport = hostport[4:]
        host, _, port = hostport.partition(":")
        try:
            return socket.gethostbyname(host), int(port.split(";")[0] or 5060)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _route_ip(dest):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((dest, 5060))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except OSError:
            return "127.0.0.1"

    # ---- utilidades

    @property
    def contact_host(self):
        if self.public and self.acc.get("allow_rewrite", True):
            return self.public
        return self.local_ip, self.port

    def contact(self):
        h, p = self.contact_host
        return f"<sip:{self.acc['user']}@{h}:{p}>"

    def media_ip(self):
        pub = self.acc.get("public_addr", "Auto")
        if pub and pub not in ("Auto", ""):
            return pub
        return self.contact_host[0] if self.public and self.acc.get("allow_rewrite", True) else self.local_ip

    def aor(self):
        return f"sip:{self.acc['user']}@{self.acc['domain']}"

    def from_hdr(self, tag):
        disp = self.acc.get("display", "")
        if self.acc.get("hide_id"):
            disp = "Anonymous"
        d = f'"{disp}" ' if disp else ""
        return f"{d}<{self.aor()}>;tag={tag}"

    def target_uri(self, number):
        number = number.strip()
        if number.startswith(("sip:", "sips:")):
            return number
        if "@" in number:
            return "sip:" + number
        number = re.sub(r"[\s\-().]", "", number)
        return f"sip:{self.acc.get('prefix', '')}{number}@{self.acc['domain']}"

    def codec_prefs(self):
        return self.cfg.get("codecs") or ["PCMA", "PCMU"]

    def _send_raw(self, data, dest):
        try:
            self.sock.sendto(data, dest)
            log.debug(">>> %s\n%s", dest, data.decode("utf-8", "replace"))
        except (OSError, AttributeError) as e:
            log.warning("falha ao enviar para %s: %s", dest, e)

    def _build(self, req):
        lines = [f"{req.method} {req.ruri} SIP/2.0",
                 f"Via: SIP/2.0/UDP {self.local_ip}:{self.port};rport;branch={req.branch}",
                 "Max-Forwards: 70"]
        lines += [f"Route: {r}" for r in req.routes]
        lines += [f"From: {req.from_}", f"To: {req.to}", f"Call-ID: {req.call_id}",
                  f"CSeq: {req.cseq} {req.method if req.method != 'ACK' else 'ACK'}"]
        if req.contact:
            lines.append(f"Contact: {self.contact()}")
        if req.auth:
            lines.append(req.auth)
        lines.append(f"User-Agent: {USER_AGENT}")
        if req.method in ("INVITE", "REGISTER", "OPTIONS", "UPDATE"):
            lines.append("Allow: INVITE, ACK, CANCEL, BYE, OPTIONS, INFO, REFER, NOTIFY, MESSAGE, UPDATE")
        lines += req.extra
        if req.body:
            lines.append(f"Content-Type: {req.ctype}")
        lines.append(f"Content-Length: {len(req.body)}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode() + req.body

    def _dest_for(self, req):
        if req.dest:
            return req.dest
        if req.routes:
            _, uri, _ = parse_addr(req.routes[0])
            hp = uri_hostport(uri)
            if hp:
                return self._resolve(f"{hp[0]}:{hp[1]}") or self.server
        return self.server

    def send_request(self, req, cb=None):
        req.branch = gen_branch()
        data = self._build(req)
        dest = self._dest_for(req)
        if req.method == "ACK":
            self._send_raw(data, dest)
            return data, dest
        tx = ClientTx(req, data, dest, cb)
        with self.lock:
            self.ctx[req.branch] = tx
        self._send_raw(data, dest)
        return tx

    def _auth_retry(self, tx, resp):
        """True se reenviou com credenciais."""
        req = tx.req
        hdr = "www-authenticate" if resp.status == 401 else "proxy-authenticate"
        chal = resp.get(hdr)
        if not chal or req.auth_tries >= 2:
            return False
        stale = parse_auth(chal).get("stale", "").lower() == "true"
        if req.auth_tries >= 1 and not stale:
            return False
        req.auth_tries += 1
        name = "Authorization" if resp.status == 401 else "Proxy-Authorization"
        login = self.acc.get("login") or self.acc["user"]
        req.auth = f"{name}: " + digest(chal, req.method, req.ruri, login, self.acc.get("password", ""))
        req.cseq += 1
        call = getattr(req, "call", None)
        if call:
            call.local_cseq = max(call.local_cseq, req.cseq)
        self.send_request(req, tx.cb)
        return True

    def _respond(self, req, code, reason, to_tag=None, extra=None, body=b"", ctype="application/sdp",
                 contact=False, store=True):
        lines = [f"SIP/2.0 {code} {reason}"]
        lines += [f"Via: {v}" for v in req.get_all("via")]
        # rport/received no primeiro Via
        if req.src:
            first = lines[1]
            if ";rport" in first and not re.search(r";rport=\d", first):
                lines[1] = first.replace(";rport", f";rport={req.src[1]};received={req.src[0]}", 1)
        if code < 300 or (100 < code < 200):
            lines += [f"Record-Route: {r}" for r in req.get_all("record-route")]
        lines.append(f"From: {req.get('from')}")
        to = req.get("to")
        if to_tag and ";tag=" not in to:
            to += f";tag={to_tag}"
        lines += [f"To: {to}", f"Call-ID: {req.call_id}", f"CSeq: {req.get('cseq')}"]
        if contact:
            lines.append(f"Contact: {self.contact()}")
        lines.append(f"User-Agent: {USER_AGENT}")
        lines += list(extra or [])
        if body:
            lines.append(f"Content-Type: {ctype}")
        lines.append(f"Content-Length: {len(body)}")
        data = ("\r\n".join(lines) + "\r\n\r\n").encode() + body
        self._send_raw(data, req.src)
        if store:
            self.stx[(req.top_branch, req.method)] = (data, req.src, time.monotonic() + 32)
        return data

    # ---- laços

    def _rx_loop(self):
        while self.running:
            try:
                r, _, _ = select.select([self.sock], [], [], 0.5)
                if not r:
                    continue
                data, addr = self.sock.recvfrom(65535)
            except (OSError, ValueError):
                break
            if not data.strip():
                continue
            msg = SipMessage.parse(data, addr)
            if not msg:
                continue
            log.debug("<<< %s\n%s", addr, data.decode("utf-8", "replace"))
            try:
                with self.lock:
                    if msg.is_request:
                        self._on_request(msg)
                    else:
                        self._on_response(msg)
            except Exception:
                log.exception("erro tratando mensagem")

    def _timer_loop(self):
        while self.running:
            time.sleep(0.05)
            now = time.monotonic()
            try:
                with self.lock:
                    self._timers(now)
            except Exception:
                log.exception("erro no timer")

    def _timers(self, now):
        for br, tx in list(self.ctx.items()):
            if tx.final:
                if now - tx.start > 32:
                    del self.ctx[br]
                continue
            if now - tx.start > 32:
                del self.ctx[br]
                fake = SipMessage()
                fake.status, fake.reason = 408, "Request Timeout"
                if tx.cb:
                    tx.cb(tx, fake)
                continue
            if now >= tx.next and not (tx.invite and tx.provisional):
                self._send_raw(tx.data, tx.dest)
                tx.interval = min(tx.interval * 2, T2) if not tx.provisional else T2
                tx.next = now + tx.interval
        for k, (_, _, exp) in list(self.stx.items()):
            if now > exp:
                del self.stx[k]
        for c in list(self.calls.values()):
            if c.uas_200 and now >= c.uas_200_next:
                if now - c.uas_200_start > 32:
                    c.uas_200 = None
                    log.warning("sem ACK, encerrando chamada %s", c.number)
                    self._send_bye(c)
                    self._end(c, "Sem ACK")
                    continue
                self._send_raw(c.uas_200, c.invite.src)
                c.uas_200_int = min(c.uas_200_int * 2, T2)
                c.uas_200_next = now + c.uas_200_int
        if self.reg_next and now >= self.reg_next:
            self.reg_next = 0
            self.register()
        ka = int(self.acc.get("keepalive") or 0)
        if ka and self.reg_state == "online" and now >= self.keep_next:
            self.keep_next = now + ka
            self._send_raw(b"\r\n\r\n", self.server)

    # ---- registro

    def _set_reg(self, state, text):
        self.reg_state = state
        self.ui.on_reg_state(state, text)

    def register(self, expires=None):
        if expires is None:
            expires = int(self.acc.get("expires") or 300)
        self.reg_cseq += 1
        req = Req("REGISTER", f"sip:{self.acc['domain']}", self.from_hdr(gen_tag()), f"<{self.aor()}>",
                  self.reg_callid, self.reg_cseq, extra=[f"Expires: {expires}"])
        req.expires = expires
        if expires:
            self._set_reg("registering", "Registrando...")
        self.send_request(req, self._on_register_resp)

    def _on_register_resp(self, tx, resp):
        if resp.status < 200:
            return
        req = tx.req
        if resp.status in (401, 407) and self._auth_retry(tx, resp):
            self.reg_cseq = req.cseq
            return
        if req.expires == 0:
            return
        if 200 <= resp.status < 300:
            via = resp.get_all("via")[0] if resp.get_all("via") else ""
            rec = re.search(r";received=([\d.]+)", via)
            rp = re.search(r";rport=(\d+)", via)
            if rec or rp:
                pub = (rec.group(1) if rec else self.local_ip, int(rp.group(1)) if rp else self.port)
                if self.acc.get("allow_rewrite", True) and pub != (self.public or (self.local_ip, self.port)) \
                        and pub != (self.local_ip, self.port):
                    log.info("endereço público detectado: %s", pub)
                    self.public = pub
                    self.register()
                    return
            exp = int(self.acc.get("expires") or 300)
            m = re.search(r"expires=(\d+)", resp.get("contact"))
            if m:
                exp = int(m.group(1))
            elif resp.get("expires").isdigit():
                exp = int(resp.get("expires"))
            self.reg_next = time.monotonic() + max(30, exp * 0.85 if exp > 60 else exp - 5)
            self.keep_next = time.monotonic() + int(self.acc.get("keepalive") or 15)
            self._set_reg("online", "Online")
        else:
            self._set_reg("error", f"{resp.status} {resp.reason}")
            self.reg_next = time.monotonic() + 30

    # ---- chamadas de saída

    def make_call(self, number):
        with self.lock:
            uri = self.target_uri(number)
            call = Call("out", number)
            call.call_id = uuid.uuid4().hex + "@" + self.local_ip
            call.local_addr = self.from_hdr(call.local_tag)
            call.remote_addr = f"<{uri}>"
            call.remote_target = uri
            call.rtp = RtpStream(int(self.cfg.get("rtp_min", 4000)), int(self.cfg.get("rtp_max", 4999)))
            self.calls[call.call_id] = call
            extra = ["Supported: replaces, norefersub"]
            if self.acc.get("hide_id"):
                extra.append("Privacy: id")
            body = self._sdp(call)
            req = Req("INVITE", uri, call.local_addr, call.remote_addr, call.call_id, call.local_cseq,
                      extra=extra, body=body, ctype="application/sdp")
            req.call = call
            call.invite_req = req
            self.send_request(req, self._on_invite_resp)
            self.ui.on_call_update(call)
            return call

    def _sdp(self, call, direction=None):
        prefs = [CODECS[n] for n in self.codec_prefs() if n in CODECS]
        if call.remote_sdp and call.rtp:     # resposta: só o codec escolhido
            prefs = [call.rtp.codec] if call.rtp.remote else prefs
        dtmf = 101
        if call.remote_sdp is not None:
            dtmf = call.remote_sdp.get("dtmf_pt")
        if self.cfg.get("dtmf") == "info":
            dtmf = None
        call.sdp_ver += 1
        return build_sdp(self.media_ip(), call.rtp.port, prefs, dtmf,
                         direction or ("sendonly" if call.hold else "sendrecv"), call.sess_id, call.sdp_ver)

    def _on_invite_resp(self, tx, resp):
        call = tx.req.call
        if call.ended:
            if 100 < resp.status < 200 and call.cancel_pending:
                call.cancel_pending = False
                call.got_provisional = True
                self._send_cancel(call)
            elif 200 <= resp.status < 300:
                self._ack_2xx(call, resp)
                self._send_bye(call)
            return
        if resp.status < 200:
            if resp.status > 100:
                call.got_provisional = True
                self._update_dialog(call, resp)
                if call.cancel_pending:
                    self._send_cancel(call)
                    return
                if resp.body and b"m=audio" in resp.body:
                    self._apply_sdp(call, resp.body)
                    call.early_media = True
                call.state = "ringing" if resp.status in (180, 183) else call.state
                self.ui.on_call_update(call)
            return
        if 200 <= resp.status < 300:
            self._update_dialog(call, resp)
            self._ack_2xx(call, resp)
            if resp.body:
                self._apply_sdp(call, resp.body)
            if call.state not in ("confirmed", "hold"):
                call.state = "confirmed"
                call.answered = call.answered or time.time()
            self.ui.on_call_update(call)
            return
        if resp.status in (401, 407) and self._auth_retry(tx, resp):
            call.invite_req = tx.req
            return
        self._end(call, f"{resp.status} {resp.reason}")

    def _update_dialog(self, call, resp):
        _, _, p = parse_addr(resp.get("to"))
        if p.get("tag"):
            call.remote_tag = p["tag"]
            call.remote_addr = resp.get("to")
        ct = resp.get_all("contact")
        if ct:
            call.remote_target = parse_addr(ct[0])[1]
        rr = resp.get_all("record-route")
        if rr:
            call.route_set = list(reversed(rr))

    def _ack_2xx(self, call, resp):
        n, _ = resp.cseq
        req = Req("ACK", call.remote_target, call.local_addr, call.remote_addr, call.call_id, n,
                  routes=call.route_set)
        call.ack_data, call.ack_dest = self.send_request(req)

    def _apply_sdp(self, call, body):
        sdp = parse_sdp(body)
        call.remote_sdp = sdp
        codec = sdp_codec(sdp, self.codec_prefs())
        if not codec or not sdp["port"]:
            log.warning("SDP sem codec compatível")
            return False
        call.remote_hold = sdp["direction"] in ("sendonly", "inactive") or sdp["ip"] == "0.0.0.0"
        dtmf = sdp["dtmf_pt"] if self.cfg.get("dtmf") != "info" else None
        call.rtp.configure(sdp["ip"], sdp["port"], codec, dtmf)
        self.hub.add(call.rtp)
        return True

    def _send_cancel(self, call):
        r = call.invite_req
        req = Req("CANCEL", r.ruri, r.from_, r.to, r.call_id, r.cseq, routes=r.routes, contact=False)
        req.branch = r.branch
        data = self._build(req)
        tx = ClientTx(req, data, self._dest_for(r), None)
        self.ctx[r.branch + "-cancel"] = tx
        self._send_raw(data, tx.dest)

    def _send_bye(self, call):
        req = Req("BYE", call.remote_target, call.local_addr, call.remote_addr, call.call_id,
                  call.next_cseq(), routes=call.route_set)
        req.call = call
        self.send_request(req, self._on_indialog_resp)

    def _on_indialog_resp(self, tx, resp):
        if resp.status in (401, 407):
            self._auth_retry(tx, resp)

    def hangup(self, call):
        with self.lock:
            if call.ended:
                return
            if call.direction == "out" and call.state in ("calling", "ringing"):
                if call.got_provisional:
                    self._send_cancel(call)
                else:
                    call.cancel_pending = True
                self._end(call, "Cancelada")
            elif call.direction == "in" and call.state == "incoming":
                self._respond(call.invite, 603, "Decline", call.local_tag)
                self._end(call, "Recusada")
            else:
                self._send_bye(call)
                self._end(call, "Encerrada")

    def _end(self, call, reason):
        if call.ended:
            return
        call.ended = time.time()
        call.reason = reason
        call.state = "ended"
        if call.rtp:
            self.hub.remove(call.rtp)
            call.rtp.close()
        log.info("chamada %s encerrada: %s", call.number, reason)
        self.ui.on_call_update(call)

        def drop():
            with self.lock:
                if self.calls.get(call.call_id) is call:
                    del self.calls[call.call_id]
        threading.Timer(32, drop).start()

    # ---- recursos da chamada

    def answer(self, call):
        with self.lock:
            if call.state != "incoming" or call.ended:
                return
            call.rtp = call.rtp or RtpStream(int(self.cfg.get("rtp_min", 4000)), int(self.cfg.get("rtp_max", 4999)))
            if call.invite.body:
                if not self._apply_sdp(call, call.invite.body):
                    self._respond(call.invite, 488, "Not Acceptable Here", call.local_tag)
                    self._end(call, "Codec incompatível")
                    return
            body = self._sdp(call)
            call.uas_200 = self._respond(call.invite, 200, "OK", call.local_tag, body=body, contact=True,
                                         extra=["Allow: INVITE, ACK, CANCEL, BYE, OPTIONS, INFO, REFER, NOTIFY, MESSAGE"])
            call.uas_200_start = time.monotonic()
            call.uas_200_next = call.uas_200_start + T1
            call.uas_200_int = T1
            call.state = "confirmed"
            call.answered = time.time()
            self.ui.on_call_update(call)

    def set_hold(self, call, hold):
        with self.lock:
            if call.ended or call.state not in ("confirmed", "hold") or call.hold == hold:
                return
            call.hold = hold
            call.rtp.hold = hold
            body = self._sdp(call)
            req = Req("INVITE", call.remote_target, call.local_addr, call.remote_addr, call.call_id,
                      call.next_cseq(), routes=call.route_set, body=body, ctype="application/sdp")
            req.call = call
            call.state = "hold" if hold else "confirmed"
            self.send_request(req, self._on_reinvite_resp)
            self.ui.on_call_update(call)

    def _on_reinvite_resp(self, tx, resp):
        call = tx.req.call
        if resp.status < 200:
            return
        if 200 <= resp.status < 300:
            self._ack_2xx(call, resp)
            if resp.body:
                self._apply_sdp(call, resp.body)
        elif resp.status in (401, 407):
            self._auth_retry(tx, resp)

    def send_dtmf(self, call, digit):
        with self.lock:
            if call.ended or call.state not in ("confirmed", "hold"):
                return
            if call.rtp and call.rtp.dtmf_pt is not None and self.cfg.get("dtmf") != "info":
                call.rtp.queue_dtmf(digit)
            else:
                req = Req("INFO", call.remote_target, call.local_addr, call.remote_addr, call.call_id,
                          call.next_cseq(), routes=call.route_set,
                          body=f"Signal={digit}\r\nDuration=160\r\n".encode(), ctype="application/dtmf-relay")
                req.call = call
                self.send_request(req, self._on_indialog_resp)

    def transfer(self, call, number):
        with self.lock:
            if call.ended:
                return
            target = self.target_uri(number)
            call.transfer_to = number
            req = Req("REFER", call.remote_target, call.local_addr, call.remote_addr, call.call_id,
                      call.next_cseq(), routes=call.route_set,
                      extra=[f"Refer-To: <{target}>", f"Referred-By: <{self.aor()}>"])
            req.call = call
            self.send_request(req, self._on_refer_resp)

    def _on_refer_resp(self, tx, resp):
        call = tx.req.call
        if resp.status < 200:
            return
        if resp.status in (401, 407) and self._auth_retry(tx, resp):
            return
        if resp.status >= 300:
            self.ui.on_transfer_result(call, False, f"{resp.status} {resp.reason}")

    def set_recording(self, call, path):
        with self.lock:
            if not call.rtp:
                return
            if call.rtp.recorder:
                call.rtp.recorder.close()
                call.rtp.recorder = None
            if path:
                call.rtp.recorder = Recorder(path)

    def send_message(self, number, text):
        with self.lock:
            uri = self.target_uri(number)
            req = Req("MESSAGE", uri, self.from_hdr(gen_tag()), f"<{uri}>", uuid.uuid4().hex, 1,
                      body=text.encode(), ctype="text/plain;charset=UTF-8", contact=False)

            def cb(tx, resp):
                if resp.status in (401, 407) and self._auth_retry(tx, resp):
                    return
                if resp.status >= 200:
                    self.ui.on_message_status(number, resp.status < 300, f"{resp.status} {resp.reason}")
            self.send_request(req, cb)

    # ---- tratamento de mensagens recebidas

    def _on_response(self, resp):
        br = resp.top_branch
        _, method = resp.cseq
        tx = self.ctx.get(br) if method != "CANCEL" else self.ctx.get(br + "-cancel")
        if method == "CANCEL":
            if tx:
                tx.final = True
            return
        if not tx:
            if method == "INVITE" and 200 <= resp.status < 300:   # 200 retransmitido
                call = self.calls.get(resp.call_id)
                if call and call.ack_data:
                    self._send_raw(call.ack_data, call.ack_dest)
            return
        if tx.final:
            if tx.invite and resp.status >= 300:
                self._ack_non2xx(tx, resp)
            elif tx.invite and resp.status >= 200:
                call = getattr(tx.req, "call", None)
                if call and call.ack_data:
                    self._send_raw(call.ack_data, call.ack_dest)
            return
        if resp.status < 200:
            tx.provisional = True
            tx.next = time.monotonic() + T2
        else:
            tx.final = True
            tx.final_resp = resp
            if tx.invite and resp.status >= 300:
                self._ack_non2xx(tx, resp)
        if tx.cb:
            tx.cb(tx, resp)

    def _ack_non2xx(self, tx, resp):
        r = tx.req
        req = Req("ACK", r.ruri, r.from_, resp.get("to"), r.call_id, r.cseq, routes=r.routes, contact=False)
        req.branch = r.branch
        self._send_raw(self._build(req), tx.dest)

    def _on_request(self, req):
        key = (req.top_branch, req.method)
        if req.method != "ACK" and key in self.stx:
            data, dest, _ = self.stx[key]
            self._send_raw(data, dest)
            return
        m = req.method
        call = self.calls.get(req.call_id)
        _, _, to_p = parse_addr(req.get("to"))
        if m == "INVITE":
            if to_p.get("tag") and call:
                self._on_reinvite(call, req)
            elif call and call.direction == "in":
                pass   # retransmissão já tratada
            else:
                self._on_new_invite(req)
        elif m == "ACK":
            if call and call.uas_200:
                call.uas_200 = None
                if req.body and not call.rtp.remote:
                    self._apply_sdp(call, req.body)
        elif m == "BYE":
            self._respond(req, 200, "OK")
            if call:
                self._end(call, "Encerrada pelo outro lado")
        elif m == "CANCEL":
            self._respond(req, 200, "OK")
            if call and call.state == "incoming":
                self._respond(call.invite, 487, "Request Terminated", call.local_tag)
                self._end(call, "Perdida")
        elif m == "OPTIONS":
            self._respond(req, 200, "OK", gen_tag() if not to_p.get("tag") else None,
                          extra=["Allow: INVITE, ACK, CANCEL, BYE, OPTIONS, INFO, REFER, NOTIFY, MESSAGE"],
                          contact=True)
        elif m == "NOTIFY":
            self._respond(req, 200, "OK")
            if call and "refer" in req.get("event").lower() and req.body:
                line = req.body.decode("utf-8", "replace").split("\r\n")[0]
                mm = re.match(r"SIP/2.0 (\d+)", line)
                if mm and int(mm.group(1)) >= 200:
                    ok = int(mm.group(1)) < 300
                    self.ui.on_transfer_result(call, ok, line)
                    if ok and not call.ended:
                        self._send_bye(call)
                        self._end(call, "Transferida")
        elif m == "INFO":
            self._respond(req, 200, "OK")
        elif m == "MESSAGE":
            self._respond(req, 200, "OK", gen_tag())
            disp, uri, _ = parse_addr(req.get("from"))
            self.ui.on_message(disp, uri_user(uri), req.body.decode("utf-8", "replace"))
        elif m == "REFER":
            self._respond(req, 603, "Decline")
        elif m == "UPDATE":
            if call and req.body:
                self._apply_sdp(call, req.body)
                self._respond(req, 200, "OK", body=self._sdp(call), contact=True)
            else:
                self._respond(req, 200, "OK", contact=True)
        else:
            self._respond(req, 501, "Not Implemented")

    def _on_new_invite(self, req):
        disp, uri, _ = parse_addr(req.get("from"))
        pai = req.get("p-asserted-identity") or req.get("remote-party-id")
        if pai:
            d2, u2, _ = parse_addr(split_commas(pai)[0])
            disp, uri = d2 or disp, u2 or uri
        self._respond(req, 100, "Trying", store=False)
        call = Call("in", uri_user(uri), disp)
        call.call_id = req.call_id
        call.invite = req
        call.local_addr = req.get("to") + f";tag={call.local_tag}"
        call.remote_addr = req.get("from")
        ct = req.get_all("contact")
        call.remote_target = parse_addr(ct[0])[1] if ct else uri
        call.route_set = req.get_all("record-route")
        aa = req.get("call-info")
        call.auto_answer = "answer-after" in aa.lower() or "auto answer" in req.get("alert-info").lower()
        if req.body:
            sdp = parse_sdp(req.body)
            if not sdp_codec(sdp, self.codec_prefs()):
                self._respond(req, 488, "Not Acceptable Here", call.local_tag)
                return
        if self.cfg.get("dnd"):
            self._respond(req, 486, "Busy Here", call.local_tag)
            call.state = "ended"
            call.ended = time.time()
            call.reason = "Não perturbe"
            self.ui.on_call_update(call)
            return
        self.calls[call.call_id] = call
        self._respond(req, 180, "Ringing", call.local_tag, contact=True)
        self.ui.on_incoming(call)

    def _on_reinvite(self, call, req):
        if req.body:
            if not self._apply_sdp(call, req.body):
                self._respond(req, 488, "Not Acceptable Here")
                return
        body = self._sdp(call)
        call.invite = req
        call.uas_200 = self._respond(req, 200, "OK", body=body, contact=True)
        call.uas_200_start = time.monotonic()
        call.uas_200_next = call.uas_200_start + T1
        call.uas_200_int = T1
        self.ui.on_call_update(call)
