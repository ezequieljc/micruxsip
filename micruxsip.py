#!/usr/bin/env python3
"""MicruxSip - softphone SIP para Linux (GTK3)."""
import array
import datetime
import json
import logging
import logging.handlers
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import wave

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango  # noqa: E402
import cairo  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sipcore  # noqa: E402
import updater  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "MicruxSip"
REPO = "ezequieljc/micruxsip"
BRANCH = "main"
REPO_URL = f"https://github.com/{REPO}"
APP_DIR = os.path.expanduser("~/.config/micruxsip")
CFG_FILE = os.path.join(APP_DIR, "config.json")
CALLS_FILE = os.path.join(APP_DIR, "calls.json")
CONTACTS_FILE = os.path.join(APP_DIR, "contacts.json")
LOG_FILE = os.path.join(APP_DIR, "micruxsip.log")
SOUND_DIR = os.path.join(APP_DIR, "sons")
ICON_FILE = os.path.join(HERE, "data", "micruxsip.png")

try:
    with open(os.path.join(HERE, "VERSION")) as _f:
        VERSION = _f.read().strip()
except OSError:
    VERSION = "0.0.0"
sipcore.USER_AGENT = f"{APP_NAME}/{VERSION}"

DEFAULT_SETTINGS = {
    "sip_port": 5060, "rtp_min": 4000, "rtp_max": 4999, "codecs": ["PCMA", "PCMU"], "dtmf": "auto",
    "mic": "", "speaker": "", "ring_device": "", "keypad_sound": True, "ring_enabled": True,
    "rec_dir": os.path.expanduser("~/Gravações MicruxSip"), "dnd": False, "aa": False, "rec": False,
    "conf": False, "mic_vol": 100, "spk_vol": 100, "always_on_top": False, "history": [],
    "check_updates": True, "last_update_check": 0,
}
DEFAULT_ACCOUNT = {
    "name": "", "server": "", "proxy": "", "user": "", "domain": "", "login": "", "password": "",
    "display": "", "voicemail": "", "prefix": "", "dialplan": "", "hide_id": False,
    "encryption": "Desabilitado", "transport": "UDP", "public_addr": "Auto", "expires": 300,
    "keepalive": 15, "publish": True, "allow_rewrite": True, "ice": False, "no_timers": False,
}

log = logging.getLogger("micruxsip")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(APP_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------- sons

def _write_wav(path, samples, rate=16000):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(array.array("h", (max(-32767, min(32767, int(s))) for s in samples)).tobytes())


def _tone(freqs, dur, rate=16000, amp=9000):
    n = int(dur * rate)
    fade = int(0.01 * rate)
    out = []
    for i in range(n):
        env = min(1.0, i / fade, (n - i) / fade)
        out.append(amp * env * sum(math.sin(2 * math.pi * f * i / rate) for f in freqs) / len(freqs))
    return out


def _silence(dur, rate=16000):
    return [0] * int(dur * rate)


DTMF_FREQ = {"1": (697, 1209), "2": (697, 1336), "3": (697, 1477), "4": (770, 1209), "5": (770, 1336),
             "6": (770, 1477), "7": (852, 1209), "8": (852, 1336), "9": (852, 1477), "*": (941, 1209),
             "0": (941, 1336), "#": (941, 1477)}


def make_sounds():
    os.makedirs(SOUND_DIR, exist_ok=True)
    if os.path.exists(os.path.join(SOUND_DIR, "v2")):
        return
    rate = 16000
    burst = []
    for i in range(int(0.4 * rate)):     # trinado: alterna 1000/1300 Hz a 20 Hz
        f = 1000 if (i // (rate // 40)) % 2 == 0 else 1300
        env = min(1.0, i / 160, (0.4 * rate - i) / 160)
        burst.append(9000 * env * math.sin(2 * math.pi * f * i / rate))
    _write_wav(os.path.join(SOUND_DIR, "ring.wav"), burst + _silence(0.2) + burst + _silence(2.0))
    _write_wav(os.path.join(SOUND_DIR, "ringback.wav"), _tone([425], 1.0, amp=6000) + _silence(4.0))
    _write_wav(os.path.join(SOUND_DIR, "busy.wav"), (_tone([425], 0.25, amp=6000) + _silence(0.25)) * 4)
    _write_wav(os.path.join(SOUND_DIR, "message.wav"), _tone([880], 0.08) + _silence(0.05) + _tone([1320], 0.12))
    for d, fr in DTMF_FREQ.items():
        name = {"*": "star", "#": "hash"}.get(d, d)
        _write_wav(os.path.join(SOUND_DIR, f"dtmf_{name}.wav"), _tone(fr, 0.12, amp=5000))
    open(os.path.join(SOUND_DIR, "v2"), "w").close()


class Sounds:
    def __init__(self):
        self.loops = {}
        self.device = None

    def _cmd(self, name):
        return ["paplay"] + ([f"--device={self.device}"] if self.device else []) + [os.path.join(SOUND_DIR, name + ".wav")]

    def play(self, name):
        try:
            subprocess.Popen(self._cmd(name), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def dtmf(self, digit):
        self.play("dtmf_" + {"*": "star", "#": "hash"}.get(digit, digit))

    def loop(self, name):
        if name in self.loops:
            return
        state = {"stop": False, "proc": None}
        self.loops[name] = state

        def run():
            while not state["stop"]:
                try:
                    state["proc"] = subprocess.Popen(self._cmd(name), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    state["proc"].wait()
                except OSError:
                    break
        threading.Thread(target=run, daemon=True).start()

    def stop(self, name):
        state = self.loops.pop(name, None)
        if state:
            state["stop"] = True
            if state["proc"]:
                try:
                    state["proc"].terminate()
                except OSError:
                    pass


# ---------------------------------------------------------------- ícones desenhados

def _pixbuf(draw, w=16, h=16):
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surf)
    draw(cr)
    return Gdk.pixbuf_get_from_surface(surf, 0, 0, w, h)


def call_icon(kind):
    color = {"in": (0.18, 0.62, 0.24), "out": (0.10, 0.42, 0.82), "missed": (0.85, 0.19, 0.15)}[kind]

    def draw(cr):
        cr.set_source_rgb(*color)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_width(2.6)
        cr.arc(12.5, 12.5, 8.5, math.pi, 1.5 * math.pi)
        cr.stroke()
        for x, y in ((4.0, 12.5), (12.5, 4.0)):
            cr.arc(x, y, 2.4, 0, 2 * math.pi)
            cr.fill()
        cr.set_line_width(1.6)
        if kind == "out":       # seta saindo ↗
            cr.move_to(8.5, 14.5); cr.line_to(14.5, 8.5); cr.stroke()
            cr.move_to(10.5, 8.5); cr.line_to(14.5, 8.5); cr.line_to(14.5, 12.5); cr.stroke()
        else:                   # seta entrando ↙
            cr.move_to(14.5, 8.5); cr.line_to(8.5, 14.5); cr.stroke()
            cr.move_to(8.5, 10.5); cr.line_to(8.5, 14.5); cr.line_to(12.5, 14.5); cr.stroke()
    return _pixbuf(draw)


def app_icon(size=64):
    def draw(cr):
        s = size / 64
        cr.scale(s, s)
        g = cairo.LinearGradient(0, 0, 0, 64)
        g.add_color_stop_rgb(0, 0.25, 0.62, 0.95)
        g.add_color_stop_rgb(1, 0.05, 0.36, 0.75)
        cr.arc(32, 32, 30, 0, 2 * math.pi)
        cr.set_source(g)
        cr.fill()
        cr.set_source_rgb(1, 1, 1)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_width(7)
        cr.arc(40, 40, 20, math.pi, 1.5 * math.pi)
        cr.stroke()
        for x, y in ((20, 40), (40, 20)):
            cr.arc(x, y, 6, 0, 2 * math.pi)
            cr.fill()
    return _pixbuf(draw, size, size)


CSS = b"""
#main, #main > box, .page, dialog, .dialog-body { background-color: #ffffff; color: #000000; }
#main { font-family: "Segoe UI", "Noto Sans", "DejaVu Sans", sans-serif; font-size: 9.5pt; }
.key { background-image: none; background-color: #fcfcfc; border: 1px solid #e1e1e1; border-radius: 4px;
       box-shadow: none; padding: 0; min-height: 31px; text-shadow: none; }
.key:hover { background-color: #f3f3f3; border-color: #d2d2d2; }
.key:active { background-color: #e4e4e4; }
#main .key .digit, #main .key > label { font-size: 15pt; color: #000000; }
.key .letters { font-size: 6.5pt; color: #555555; }
#main .fkey > label { font-size: 13pt; color: #7a7a7a; }
.callbtn { background-image: none; background-color: #e3e3e3; border: 1px solid #dadada; border-radius: 4px;
           box-shadow: none; min-height: 34px; text-shadow: none; }
.callbtn label { color: #6a6a6a; font-size: 11pt; }
.callbtn:hover { background-color: #d8d8d8; }
.callbtn.hangup { background-color: #e81123; border-color: #c50f1f; }
.callbtn.hangup:hover { background-color: #c50f1f; }
.callbtn.answer { background-color: #16a34a; border-color: #15803d; }
.callbtn.answer:hover { background-color: #15803d; }
.callbtn.hangup label, .callbtn.answer label { color: #ffffff; }
.iconbtn { background-image: none; background-color: #f5f5f5; border: 1px solid #e3e3e3; border-radius: 4px;
           box-shadow: none; min-width: 34px; min-height: 34px; padding: 0; color: #888888; }
.iconbtn:hover { background-color: #ebebeb; }
.iconbtn:disabled { background-color: #f5f5f5; color: #bbbbbb; }
.flaticon { background-image: none; background-color: #f5f5f5; border: 1px solid #e3e3e3; border-radius: 3px;
            box-shadow: none; padding: 2px; min-height: 0; min-width: 0; color: #666666; }
.flaticon.muted { background-color: #fde2e2; color: #c50f1f; }
.pm { color: #555555; font-size: 9pt; }
.toggle-sm { background-image: none; background-color: #fbfbfb; border: 1px solid #e1e1e1; border-radius: 3px;
             padding: 2px 5px; min-height: 0; min-width: 0; box-shadow: none; text-shadow: none; }
.toggle-sm label { font-size: 8pt; color: #000000; }
.toggle-sm:checked { background-color: #cce4f7; border-color: #0078d4; }
.toggle-sm:checked label { color: #003e73; }
.statusbar { background-color: #f0f0f0; border-top: 1px solid #dcdcdc; }
.statusbar label { font-size: 9pt; color: #000000; }
.statusbar separator { background-color: #d0d0d0; min-width: 1px; }
scale trough { background-image: none; background-color: #e6e6e6; border: 1px solid #d0d0d0; min-height: 14px;
               border-radius: 1px; }
scale highlight { background-image: none; background-color: transparent; border: none; }
scale slider { background-image: none; background-color: #6b6b6b; border: none; border-radius: 1px;
               min-width: 6px; min-height: 18px; margin: -3px 0; box-shadow: none; }
#main notebook > header { background-color: #f0f0f0; border: none; padding: 0; box-shadow: none; }
#main notebook > header.top { border-bottom: 1px solid #d6d6d6; }
#main notebook > header tab { min-height: 0; padding: 3px 5px; margin: 3px 0 -1px 0; border: 1px solid transparent;
                              background-color: transparent; box-shadow: none; }
#main notebook > header tab label { color: #000000; font-size: 9pt; }
#main notebook > header tab:checked { background-color: #ffffff; border-color: #d6d6d6; border-bottom-color: #ffffff; }
#main notebook > stack { background-color: #ffffff; }
.menubtn { padding: 0 5px; min-height: 0; min-width: 0; border: 1px solid #c8c8c8; border-radius: 2px;
           background-image: none; background-color: #ffffff; margin: 3px 4px; box-shadow: none; }
.menubtn label { font-size: 7pt; color: #000000; }
.tabclose { padding: 0; min-height: 0; min-width: 0; border: none; background: none; box-shadow: none; }
.tabclose label { font-size: 8pt; color: #888888; }
#main combobox entry, #main combobox button { background-image: none; background-color: #ffffff; border-color: #c6c6c6;
                                              min-height: 30px; font-size: 12pt; color: #000000; box-shadow: none; }
#main treeview, #main treeview.view { background-color: #ffffff; color: #000000; font-size: 9.5pt; }
#main treeview:selected { background-color: #cce4f7; color: #000000; }
#main treeview header button { background-image: none; background-color: #ffffff; border: none;
                               border-right: 1px solid #e5e5e5; border-bottom: 1px solid #e5e5e5; box-shadow: none; padding: 2px 4px; }
#main treeview header button label { font-weight: normal; color: #000000; }
#main entry { background-image: none; background-color: #ffffff; color: #000000; border-color: #c6c6c6; box-shadow: none; }
.callname { font-size: 14pt; font-weight: bold; }
.callnum { color: #666666; font-size: 10pt; }
.callstatus { color: #0067c0; }
.calltime { font-size: 12pt; color: #333333; }
.qmark { color: #0066cc; }
.popup { background-color: #ffffff; }
.popup label { color: #000000; }
"""


def css_class(widget, *names):
    for n in names:
        widget.get_style_context().add_class(n)
    return widget


def icon(name, size=Gtk.IconSize.BUTTON):
    return Gtk.Image.new_from_icon_name(name, size)


# ---------------------------------------------------------------- ponte thread -> GTK

class UiBridge:
    def __init__(self, win):
        self.win = win

    def __getattr__(self, name):
        target = getattr(self.win, name)

        def call(*a):
            GLib.idle_add(lambda: (target(*a), False)[1])
        return call


# ---------------------------------------------------------------- diálogos

class AccountDialog(Gtk.Dialog):
    FIELDS = [("name", "Nome da Conta", "Nome que aparece no título e na barra de status."),
              ("server", "Servidor SIP", "Endereço do PABX/servidor SIP (host ou host:porta)."),
              ("proxy", "Proxy SIP", "Proxy de saída opcional (host:porta)."),
              None,
              ("user", "Usuário*", "Ramal / usuário SIP."),
              ("domain", "Domínio*", "Domínio SIP (normalmente igual ao servidor)."),
              ("login", "Login", "Usuário de autenticação (se diferente do usuário)."),
              ("password", "Senha", "Senha do ramal."),
              None,
              ("display", "Nome de Exibição", "Nome mostrado para quem recebe sua ligação."),
              ("voicemail", "N° do Correio de Voz", "Número para acessar o correio de voz."),
              ("prefix", "Prefixo de Discagem", "Prefixo adicionado a todos os números discados."),
              ("dialplan", "Plano de discagem", "Reservado para uso futuro.")]

    def __init__(self, parent, acc, can_remove):
        super().__init__(title="Conta", transient_for=parent, modal=True)
        self.set_default_size(350, -1)
        self.set_resizable(False)
        self.removed = False
        self.acc = dict(DEFAULT_ACCOUNT, **acc)
        area = self.get_content_area()
        css_class(area, "dialog-body")
        grid = Gtk.Grid(column_spacing=8, row_spacing=6, margin=12)
        area.add(grid)
        self.w = {}
        row = 0

        def qmark(tip):
            q = Gtk.Label()
            q.set_markup("<u>?</u>")
            css_class(q, "qmark")
            q.set_tooltip_text(tip)
            return q

        def add_row(label, widget, tip, wide=True):
            nonlocal row
            lab = Gtk.Label(label=label, xalign=1)
            grid.attach(lab, 0, row, 1, 1)
            grid.attach(widget, 1, row, 2 if wide else 1, 1)
            grid.attach(qmark(tip), 3, row, 1, 1)
            row += 1

        for f in self.FIELDS:
            if f is None:
                grid.attach(Gtk.Box(height_request=6), 0, row, 1, 1)
                row += 1
                continue
            key, label, tip = f
            e = Gtk.Entry(text=str(self.acc.get(key, "")), hexpand=True, width_chars=22)
            if key == "password":
                e.set_visibility(False)
            self.w[key] = e
            add_row(label, e, tip)

        self.w["hide_id"] = Gtk.CheckButton(label="Ocultar identificador de chamadas", active=self.acc["hide_id"])
        grid.attach(self.w["hide_id"], 1, row, 2, 1)
        grid.attach(qmark("Envia a chamada como anônima (Privacy: id)."), 3, row, 1, 1)
        row += 1
        grid.attach(Gtk.Box(height_request=6), 0, row, 1, 1)
        row += 1
        for key, label, opts, tip in [
                ("encryption", "Criptografia", ["Desabilitado"], "SRTP não suportado nesta versão."),
                ("transport", "Transporte", ["UDP"], "Transporte SIP (UDP)."),
                ("public_addr", "Endereço Público", ["Auto"], "Auto: detecta via rport/received. "
                                                               "Pode digitar um IP fixo.")]:
            c = Gtk.ComboBoxText.new_with_entry() if key == "public_addr" else Gtk.ComboBoxText()
            for o in opts:
                c.append_text(o)
            if key == "public_addr":
                c.get_child().set_text(self.acc.get(key) or "Auto")
            else:
                c.set_active(0)
            self.w[key] = c
            add_row(label, c, tip)

        hb = Gtk.Box(spacing=6)
        self.w["expires"] = Gtk.SpinButton.new_with_range(30, 3600, 10)
        self.w["expires"].set_value(int(self.acc["expires"]))
        self.w["keepalive"] = Gtk.SpinButton.new_with_range(0, 300, 1)
        self.w["keepalive"].set_value(int(self.acc["keepalive"]))
        hb.pack_start(self.w["expires"], False, False, 0)
        hb.pack_end(self.w["keepalive"], False, False, 0)
        hb.pack_end(Gtk.Label(label="Manter ativo"), False, False, 0)
        add_row("Atualizar Registro", hb, "Intervalo de registro (s) / keep-alive (s).")
        for key, label, tip in [("publish", "Publicar presença", "Presença (sem efeito nesta versão)."),
                                ("allow_rewrite", "Permitir reescrever IP", "Usa o IP/porta vistos pelo servidor no Contact."),
                                ("ice", "ICE", "Sem efeito nesta versão."),
                                ("no_timers", "Desabilitar timers de sessão", "Session timers não são usados.")]:
            cb = Gtk.CheckButton(label=label, active=bool(self.acc[key]))
            self.w[key] = cb
            grid.attach(cb, 1, row, 2, 1)
            grid.attach(qmark(tip), 3, row, 1, 1)
            row += 1

        bottom = Gtk.Box(spacing=8, margin=12, margin_top=0)
        area.add(bottom)
        if can_remove:
            rm = Gtk.LinkButton(uri="", label="Remover conta")
            rm.connect("activate-link", self._remove)
            bottom.pack_start(rm, False, False, 0)
        cancel = Gtk.Button(label="Cancelar", width_request=90)
        cancel.connect("clicked", lambda *_: self.response(Gtk.ResponseType.CANCEL))
        save = Gtk.Button(label="Salvar", width_request=90)
        save.connect("clicked", self._save)
        bottom.pack_end(cancel, False, False, 0)
        bottom.pack_end(save, False, False, 0)
        self.show_all()

    def _remove(self, *_):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.YES_NO, text="Remover esta conta?")
        if d.run() == Gtk.ResponseType.YES:
            self.removed = True
            self.response(Gtk.ResponseType.OK)
        d.destroy()
        return True

    def _save(self, *_):
        for k, w in self.w.items():
            if isinstance(w, Gtk.SpinButton):
                self.acc[k] = int(w.get_value())
            elif isinstance(w, Gtk.CheckButton):
                self.acc[k] = w.get_active()
            elif isinstance(w, Gtk.ComboBoxText):
                self.acc[k] = w.get_child().get_text() if w.get_has_entry() else w.get_active_text()
            else:
                self.acc[k] = w.get_text().strip()
        if not self.acc["user"] or not self.acc["domain"]:
            d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
                                  buttons=Gtk.ButtonsType.OK, text="Preencha Usuário e Domínio.")
            d.run()
            d.destroy()
            return
        if not self.acc["server"]:
            self.acc["server"] = self.acc["domain"]
        if not self.acc["name"]:
            self.acc["name"] = self.acc["user"]
        self.response(Gtk.ResponseType.OK)


class SettingsDialog(Gtk.Dialog):
    def __init__(self, parent, s):
        super().__init__(title="Configurações", transient_for=parent, modal=True)
        self.s = dict(s)
        self.set_resizable(False)
        area = self.get_content_area()
        css_class(area, "dialog-body")
        grid = Gtk.Grid(column_spacing=8, row_spacing=6, margin=12)
        area.add(grid)
        row = 0

        def add(label, w):
            nonlocal row
            grid.attach(Gtk.Label(label=label, xalign=1), 0, row, 1, 1)
            grid.attach(w, 1, row, 1, 1)
            row += 1

        sinks, sources = sipcore.pa_sinks(), sipcore.pa_sources()

        def dev_combo(items, cur, filt=None):
            c = Gtk.ComboBoxText(hexpand=True)
            c.append("", "Padrão")
            for it in items:
                if filt is None or filt(it):
                    c.append(it, it if len(it) < 48 else it[:45] + "…")
            c.set_active_id(cur if cur in items else "")
            return c

        self.ring = dev_combo(sinks, s["ring_device"])
        self.spk = dev_combo(sinks, s["speaker"])
        self.mic = dev_combo(sources, s["mic"], lambda x: not x.endswith(".monitor"))
        add("Toque", self.ring)
        add("Alto-falante", self.spk)
        add("Microfone", self.mic)
        self.codecs = Gtk.ComboBoxText()
        self.codecs.append("PCMA,PCMU", "G.711 A-law, G.711 u-law")
        self.codecs.append("PCMU,PCMA", "G.711 u-law, G.711 A-law")
        self.codecs.append("PCMA", "Somente G.711 A-law")
        self.codecs.append("PCMU", "Somente G.711 u-law")
        self.codecs.set_active_id(",".join(s["codecs"]))
        add("Codecs", self.codecs)
        self.dtmf = Gtk.ComboBoxText()
        for k, v in (("auto", "Automático"), ("rfc2833", "RFC 2833"), ("info", "SIP INFO")):
            self.dtmf.append(k, v)
        self.dtmf.set_active_id(s["dtmf"])
        add("Método DTMF", self.dtmf)
        self.port = Gtk.SpinButton.new_with_range(0, 65535, 1)
        self.port.set_value(int(s["sip_port"]))
        add("Porta SIP local (0=auto)", self.port)
        rb = Gtk.Box(spacing=4)
        self.rmin = Gtk.SpinButton.new_with_range(1024, 65000, 2)
        self.rmin.set_value(int(s["rtp_min"]))
        self.rmax = Gtk.SpinButton.new_with_range(1026, 65534, 2)
        self.rmax.set_value(int(s["rtp_max"]))
        rb.pack_start(self.rmin, True, True, 0)
        rb.pack_start(Gtk.Label(label="–"), False, False, 0)
        rb.pack_start(self.rmax, True, True, 0)
        add("Portas RTP", rb)
        self.recdir = Gtk.FileChooserButton(title="Pasta de gravações", action=Gtk.FileChooserAction.SELECT_FOLDER)
        os.makedirs(s["rec_dir"], exist_ok=True)
        self.recdir.set_filename(s["rec_dir"])
        add("Pasta de gravações", self.recdir)
        self.keypad = Gtk.CheckButton(label="Som do teclado", active=s["keypad_sound"])
        grid.attach(self.keypad, 1, row, 1, 1)
        row += 1
        self.ringon = Gtk.CheckButton(label="Tocar toque ao receber chamada", active=s["ring_enabled"])
        grid.attach(self.ringon, 1, row, 1, 1)
        row += 1
        self.updates = Gtk.CheckButton(label="Procurar atualizações ao iniciar", active=s["check_updates"])
        grid.attach(self.updates, 1, row, 1, 1)
        self.add_button("Cancelar", Gtk.ResponseType.CANCEL)
        self.add_button("Salvar", Gtk.ResponseType.OK)
        self.show_all()

    def result(self):
        s = self.s
        s["ring_device"] = self.ring.get_active_id() or ""
        s["speaker"] = self.spk.get_active_id() or ""
        s["mic"] = self.mic.get_active_id() or ""
        s["codecs"] = (self.codecs.get_active_id() or "PCMA,PCMU").split(",")
        s["dtmf"] = self.dtmf.get_active_id() or "auto"
        s["sip_port"] = int(self.port.get_value())
        s["rtp_min"], s["rtp_max"] = int(self.rmin.get_value()), int(self.rmax.get_value())
        s["rec_dir"] = self.recdir.get_filename() or s["rec_dir"]
        s["keypad_sound"] = self.keypad.get_active()
        s["ring_enabled"] = self.ringon.get_active()
        s["check_updates"] = self.updates.get_active()
        return s


def ask_text(parent, title, fields):
    """fields: [(rótulo, valor)] -> lista de textos ou None"""
    d = Gtk.Dialog(title=title, transient_for=parent, modal=True)
    d.add_button("Cancelar", Gtk.ResponseType.CANCEL)
    d.add_button("OK", Gtk.ResponseType.OK)
    d.set_default_response(Gtk.ResponseType.OK)
    grid = Gtk.Grid(column_spacing=8, row_spacing=6, margin=12)
    d.get_content_area().add(grid)
    entries = []
    for i, (lab, val) in enumerate(fields):
        grid.attach(Gtk.Label(label=lab, xalign=1), 0, i, 1, 1)
        e = Gtk.Entry(text=val, activates_default=True, width_chars=24)
        grid.attach(e, 1, i, 1, 1)
        entries.append(e)
    d.show_all()
    r = d.run()
    out = [e.get_text().strip() for e in entries] if r == Gtk.ResponseType.OK else None
    d.destroy()
    return out


# ---------------------------------------------------------------- janelas auxiliares

class IncomingPopup(Gtk.Window):
    def __init__(self, win, call, name):
        super().__init__(title="Chamada recebida")
        self.set_keep_above(True)
        self.set_urgency_hint(True)
        self.set_default_size(300, -1)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_icon(app_icon())
        css_class(self, "popup")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=14)
        self.add(box)
        top = Gtk.Box(spacing=12)
        top.pack_start(Gtk.Image.new_from_pixbuf(app_icon(48)), False, False, 0)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        n = Gtk.Label(label=name, xalign=0)
        css_class(n, "callname")
        info.pack_start(n, False, False, 0)
        if name != call.number:
            num = Gtk.Label(label=call.number, xalign=0)
            css_class(num, "callnum")
            info.pack_start(num, False, False, 0)
        st = Gtk.Label(label="Chamada recebida...", xalign=0)
        css_class(st, "callstatus")
        info.pack_start(st, False, False, 0)
        top.pack_start(info, True, True, 0)
        box.pack_start(top, False, False, 0)
        bb = Gtk.Box(spacing=6, homogeneous=True, margin_top=6)
        a = css_class(Gtk.Button(label="Atender"), "callbtn", "answer")
        r = css_class(Gtk.Button(label="Rejeitar"), "callbtn", "hangup")
        a.connect("clicked", lambda *_: win.answer_call(call))
        r.connect("clicked", lambda *_: win.hangup_call(call))
        bb.pack_start(a, True, True, 0)
        bb.pack_start(r, True, True, 0)
        box.pack_start(bb, False, False, 0)
        self.connect("delete-event", lambda *_: (win.hangup_call(call), True)[1])
        self.show_all()
        self.present()


class MessageWindow(Gtk.Window):
    def __init__(self, win, number):
        super().__init__(title=f"Mensagem - {win.lookup_name(number)}")
        self.win, self.number = win, number
        self.set_default_size(320, 360)
        self.set_icon(app_icon())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=6)
        self.add(box)
        sw = Gtk.ScrolledWindow(vexpand=True)
        self.view = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.buf = self.view.get_buffer()
        self.buf.create_tag("me", foreground="#0b57d0", weight=Pango.Weight.BOLD)
        self.buf.create_tag("them", foreground="#1e8e3e", weight=Pango.Weight.BOLD)
        self.buf.create_tag("info", foreground="#888888", scale=0.85)
        sw.add(self.view)
        box.pack_start(sw, True, True, 0)
        hb = Gtk.Box(spacing=4)
        self.entry = Gtk.Entry(hexpand=True)
        self.entry.connect("activate", self._send)
        b = Gtk.Button(label="Enviar")
        b.connect("clicked", self._send)
        hb.pack_start(self.entry, True, True, 0)
        hb.pack_start(b, False, False, 0)
        box.pack_start(hb, False, False, 0)
        self.connect("destroy", lambda *_: win.msg_windows.pop(number, None))
        self.show_all()

    def add_line(self, who, text, tag):
        end = self.buf.get_end_iter()
        self.buf.insert_with_tags_by_name(end, f"{who} ({time.strftime('%H:%M')}): ", tag)
        self.buf.insert(self.buf.get_end_iter(), text + "\n")
        self.view.scroll_to_iter(self.buf.get_end_iter(), 0, False, 0, 0)

    def info(self, text):
        self.buf.insert_with_tags_by_name(self.buf.get_end_iter(), text + "\n", "info")

    def _send(self, *_):
        t = self.entry.get_text().strip()
        if not t:
            return
        self.entry.set_text("")
        self.add_line("Eu", t, "me")
        self.win.engine.send_message(self.number, t)


# ---------------------------------------------------------------- aba de chamada

class CallView(Gtk.Box):
    def __init__(self, win, call):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        css_class(self, "page")
        self.win, self.call = win, call
        self.name = Gtk.Label(xalign=0.5, ellipsize=Pango.EllipsizeMode.END)
        css_class(self.name, "callname")
        self.num = Gtk.Label(xalign=0.5)
        css_class(self.num, "callnum")
        self.status = Gtk.Label(xalign=0.5)
        css_class(self.status, "callstatus")
        self.timer = Gtk.Label(label="00:00", xalign=0.5)
        css_class(self.timer, "calltime")
        for w in (self.name, self.num, self.status, self.timer):
            self.pack_start(w, False, False, 0)
        grid = Gtk.Grid(row_spacing=3, column_spacing=3, row_homogeneous=True, column_homogeneous=True, margin_top=4)
        for i, d in enumerate("123456789*0#"):
            b = css_class(Gtk.Button(label=d), "key")
            b.get_child().get_style_context().add_class("digit")
            b.connect("clicked", lambda _b, d=d: win.dtmf(self.call, d))
            grid.attach(b, i % 3, i // 3, 1, 1)
        self.pack_start(grid, True, True, 0)
        row = Gtk.Box(spacing=3, homogeneous=True)
        self.hold_b = css_class(Gtk.ToggleButton(label="Espera"), "toggle-sm")
        self.hold_h = self.hold_b.connect("toggled", lambda b: win.engine.set_hold(self.call, b.get_active()))
        self.xfer_b = css_class(Gtk.Button(label="Transferir"), "toggle-sm")
        self.xfer_b.connect("clicked", lambda *_: win.transfer_dialog(self.call))
        self.rec_b = css_class(Gtk.ToggleButton(label="Gravar"), "toggle-sm")
        self.rec_h = self.rec_b.connect("toggled", lambda b: win.set_call_recording(self.call, b.get_active()))
        for b in (self.hold_b, self.xfer_b, self.rec_b):
            row.pack_start(b, True, True, 0)
        self.pack_start(row, False, False, 0)
        self.btns = Gtk.Box(spacing=4, homogeneous=True)
        self.answer_b = css_class(Gtk.Button(label="Atender"), "callbtn", "answer")
        self.answer_b.connect("clicked", lambda *_: win.answer_call(self.call))
        self.hang_b = css_class(Gtk.Button(label="Desligar"), "callbtn", "hangup")
        self.hang_b.connect("clicked", lambda *_: win.hangup_call(self.call))
        self.btns.pack_start(self.answer_b, True, True, 0)
        self.btns.pack_start(self.hang_b, True, True, 0)
        self.pack_start(self.btns, False, False, 0)
        # rótulo da aba
        self.tab = Gtk.Box(spacing=3)
        self.tab_label = Gtk.Label(max_width_chars=10, ellipsize=Pango.EllipsizeMode.END)
        x = css_class(Gtk.Button(label="✕"), "tabclose")
        x.set_relief(Gtk.ReliefStyle.NONE)
        x.connect("clicked", lambda *_: win.hangup_call(self.call) if not self.call.ended else win.close_call_tab(self.call))
        self.tab.pack_start(self.tab_label, False, False, 0)
        self.tab.pack_start(x, False, False, 0)
        self.tab.show_all()
        self.show_all()
        self.update()

    STATUS = {"calling": "Chamando...", "ringing": "Tocando...", "incoming": "Chamada recebida",
              "confirmed": "Em chamada", "hold": "Em espera", "ended": "Encerrada"}

    def update(self):
        c = self.call
        name = self.win.lookup_name(c.number, c.display)
        self.name.set_text(name)
        self.num.set_text(c.number if name != c.number else "")
        st = self.STATUS.get(c.state, c.state)
        if c.state == "confirmed" and c.remote_hold:
            st = "Em espera (remoto)"
        if c.state == "ended":
            st = c.reason or st
        if c.rtp and c.rtp.recorder:
            st += "  ● REC"
        self.status.set_text(st)
        self.tab_label.set_text(name)
        self.answer_b.set_visible(c.state == "incoming")
        self.hang_b.set_label("Rejeitar" if c.state == "incoming" else "Desligar")
        self.hang_b.set_sensitive(c.state != "ended")
        live = c.state in ("confirmed", "hold")
        for b in (self.hold_b, self.xfer_b, self.rec_b):
            b.set_sensitive(live)
        self.hold_b.handler_block(self.hold_h)
        self.hold_b.set_active(c.hold)
        self.hold_b.handler_unblock(self.hold_h)
        self.rec_b.handler_block(self.rec_h)
        self.rec_b.set_active(bool(c.rtp and c.rtp.recorder))
        self.rec_b.handler_unblock(self.rec_h)
        self.tick()

    def tick(self):
        c = self.call
        if c.answered:
            secs = int((c.ended or time.time()) - c.answered)
            self.timer.set_text(f"{secs // 3600:d}:{secs // 60 % 60:02d}:{secs % 60:02d}" if secs >= 3600
                                else f"{secs // 60:02d}:{secs % 60:02d}")
        else:
            self.timer.set_text("")


# ---------------------------------------------------------------- janela principal

class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.app = app
        self.set_name("main")
        self.set_default_size(245, 455)
        self.set_size_request(245, 455)
        self.set_resizable(False)          # tamanho fixo: gerenciadores lado a lado (i3) deixam flutuante
        self.set_icon(app_icon())
        self.cfg = load_json(CFG_FILE, {})
        self.cfg.setdefault("accounts", [])
        self.cfg.setdefault("active", 0)
        self.cfg.setdefault("enabled", True)
        self.s = dict(DEFAULT_SETTINGS, **self.cfg.get("settings", {}))
        self.cfg["settings"] = self.s
        self.calls_log = load_json(CALLS_FILE, [])
        self.contacts = load_json(CONTACTS_FILE, [])
        self.sounds = Sounds()
        self.sounds.device = self.s["ring_device"] or None
        self.engine = sipcore.SipEngine(UiBridge(self))
        self.views = {}
        self.popups = {}
        self.msg_windows = {}
        self.reg_state = "offline"
        self.icons = {k: call_icon(k) for k in ("in", "out", "missed")}

        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), prov,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self.accel = Gtk.AccelGroup()
        self.add_accel_group(self.accel)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(root)
        self.nb = Gtk.Notebook()
        self.nb.set_scrollable(True)
        root.pack_start(self.nb, True, True, 0)
        self.nb.append_page(self._phone_page(), Gtk.Label(label="Telefone"))
        self.nb.append_page(self._log_page(), Gtk.Label(label="Registros"))
        self.nb.append_page(self._contacts_page(), Gtk.Label(label="Contatos"))
        mb = css_class(Gtk.MenuButton(), "menubtn")
        mb.add(Gtk.Label(label="▼"))
        self.menu = self._build_menu()
        mb.set_popup(self.menu)
        mb.show_all()
        self.nb.set_action_widget(mb, Gtk.PackType.END)
        root.pack_start(self._statusbar(), False, False, 0)

        self.connect("key-press-event", self._on_key)
        self.connect("delete-event", self._on_close)
        GLib.timeout_add(1000, self._tick)
        self.show_all()
        self._apply_audio()
        self.set_keep_above(self.s["always_on_top"])
        self.refresh_log()
        self.refresh_contacts()
        self.refresh_history()
        self.update_title()
        self.update_call_button()
        if not self.cfg["accounts"]:
            GLib.idle_add(self._first_run)
        else:
            self.start_engine()
        if self.s["check_updates"] and time.time() - self.s["last_update_check"] > 6 * 3600:
            GLib.timeout_add_seconds(5, lambda: (self.check_updates(manual=False), False)[1])

    # ---- construção

    def _phone_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin=6)
        css_class(page, "page")
        self.combo = Gtk.ComboBoxText.new_with_entry()
        self.entry = self.combo.get_child()
        self.entry.connect("activate", lambda *_: self.on_call_button())
        self.entry.connect("changed", lambda *_: self.update_call_button())
        page.pack_start(self.combo, False, False, 0)
        grid = Gtk.Grid(row_spacing=3, column_spacing=3, row_homogeneous=True, column_homogeneous=True)
        keys = [("1", ""), ("2", "ABC"), ("3", "DEF"), ("4", "GHI"), ("5", "JKL"), ("6", "MNO"),
                ("7", "PQRS"), ("8", "TUV"), ("9", "WXYZ"), ("*", ""), ("0", ""), ("#", "")]
        for i, (d, letters) in enumerate(keys):
            b = css_class(Gtk.Button(), "key")
            hb = Gtk.Box(spacing=3, halign=Gtk.Align.START, margin_start=14)
            dl = css_class(Gtk.Label(label=d), "digit")
            hb.pack_start(dl, False, False, 0)
            if letters:
                ll = css_class(Gtk.Label(label=letters, valign=Gtk.Align.CENTER, margin_top=3), "letters")
                hb.pack_start(ll, False, False, 0)
            b.add(hb)
            b.connect("clicked", lambda _b, d=d: self.on_key_digit(d))
            grid.attach(b, i % 3, i // 3, 1, 1)
        for i, (lab, cb, tip) in enumerate([("R", self.redial, "Rediscar"), ("+", lambda: self._insert("+"), "+"),
                                            ("C", lambda: self.entry.set_text(""), "Limpar")]):
            b = css_class(Gtk.Button(label=lab), "key", "fkey")
            b.set_tooltip_text(tip)
            b.connect("clicked", lambda _b, cb=cb: cb())
            grid.attach(b, i, 4, 1, 1)
        page.pack_start(grid, True, True, 0)

        row = Gtk.Box(spacing=4, margin_top=6)
        vid = css_class(Gtk.Button(), "iconbtn")
        vid.add(icon("camera-video-symbolic"))
        vid.set_sensitive(False)
        vid.set_tooltip_text("Vídeo não suportado")
        self.call_btn = css_class(Gtk.Button(label="Chamar"), "callbtn")
        self.call_btn.connect("clicked", lambda *_: self.on_call_button())
        msg = css_class(Gtk.Button(), "iconbtn")
        msg.add(icon("mail-unread-symbolic"))
        msg.set_tooltip_text("Mensagem")
        msg.connect("clicked", lambda *_: self.open_message(self.entry.get_text().strip()))
        row.pack_start(vid, False, False, 0)
        row.pack_start(self.call_btn, True, True, 0)
        row.pack_start(msg, False, False, 0)
        page.pack_start(row, False, False, 0)

        def vol_row(icon_name, muted_icon, key, attr):
            r = Gtk.Box(spacing=6, margin_top=4)
            btn = css_class(Gtk.Button(), "flaticon")
            img = icon(icon_name)
            btn.add(img)

            def toggle(*_):
                m = not getattr(self.engine.hub, attr)
                setattr(self.engine.hub, attr, m)
                img.set_from_icon_name(muted_icon if m else icon_name, Gtk.IconSize.BUTTON)
                (btn.get_style_context().add_class if m else btn.get_style_context().remove_class)("muted")
            btn.connect("clicked", toggle)
            btn.set_tooltip_text("Mudo")
            r.pack_start(btn, False, False, 0)
            r.pack_start(css_class(Gtk.Label(label="−"), "pm"), False, False, 0)
            sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
            sc.set_draw_value(False)
            sc.set_value(self.s[key])
            sc.connect("value-changed", lambda w: self._set_volume(key, w.get_value()))
            r.pack_start(sc, True, True, 0)
            r.pack_start(css_class(Gtk.Label(label="+"), "pm"), False, False, 0)
            return r
        page.pack_start(vol_row("audio-volume-high-symbolic", "audio-volume-muted-symbolic", "spk_vol", "spk_mute"),
                        False, False, 0)
        page.pack_start(vol_row("audio-input-microphone-symbolic", "microphone-sensitivity-muted-symbolic",
                                "mic_vol", "mic_mute"), False, False, 0)

        tg = Gtk.Box(spacing=3, halign=Gtk.Align.END, margin_top=6)
        self.toggles = {}
        for key, lab, tip in [("dnd", "DND", "Não perturbe"), ("aa", "AA", "Atendimento automático"),
                              ("conf", "CONF", "Conferência (une as chamadas ativas)"),
                              ("rec", "REC", "Gravar chamadas")]:
            b = css_class(Gtk.ToggleButton(label=lab), "toggle-sm")
            b.set_tooltip_text(tip)
            b.set_active(bool(self.s[key]))
            b.connect("toggled", lambda w, k=key: self._on_toggle(k, w.get_active()))
            self.toggles[key] = b
            tg.pack_start(b, False, False, 0)
        page.pack_start(tg, False, False, 0)
        return page

    def _search_row(self, on_change, add_cb=None):
        r = Gtk.Box(spacing=4, margin=4)
        r.pack_start(icon("edit-find-symbolic"), False, False, 2)
        e = Gtk.Entry(hexpand=True)
        e.connect("changed", lambda w: on_change(w.get_text().strip().lower()))
        r.pack_start(e, True, True, 0)
        if add_cb:
            b = css_class(Gtk.Button(), "flaticon")
            b.add(icon("list-add-symbolic"))
            b.set_tooltip_text("Adicionar contato")
            b.connect("clicked", lambda *_: add_cb())
            r.pack_start(b, False, False, 0)
        return r

    def _tree(self, store, with_icon):
        flt = store.filter_new()
        tv = Gtk.TreeView(model=flt, headers_visible=True, enable_search=False)
        tv.set_tooltip_column(3)
        col = Gtk.TreeViewColumn("Nome")
        if with_icon:
            pr = Gtk.CellRendererPixbuf()
            col.pack_start(pr, False)
            col.add_attribute(pr, "pixbuf", 0)
        tr = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
        col.pack_start(tr, True)
        col.add_attribute(tr, "text", 1)
        col.set_expand(True)
        col.set_resizable(True)
        tv.append_column(col)
        c2 = Gtk.TreeViewColumn("Número", Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END), text=2)
        c2.set_fixed_width(80)
        c2.set_resizable(True)
        tv.append_column(c2)
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.add(tv)
        return tv, flt, sw

    def _log_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        css_class(page, "page")
        self.log_store = Gtk.ListStore(GdkPixbuf.Pixbuf, str, str, str, int)
        self.log_tv, self.log_filter, sw = self._tree(self.log_store, True)
        self.log_q = ""
        self.log_filter.set_visible_func(lambda m, it, _: not self.log_q or self.log_q in (m[it][1] + m[it][2]).lower())
        self.log_tv.connect("row-activated", lambda tv, path, _c: self.dial(tv.get_model()[path][2]))
        self.log_tv.connect("button-press-event", self._log_menu)
        page.pack_start(sw, True, True, 0)
        page.pack_start(self._search_row(lambda q: (setattr(self, "log_q", q), self.log_filter.refilter())), False, False, 0)
        return page

    def _contacts_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        css_class(page, "page")
        self.ct_store = Gtk.ListStore(GdkPixbuf.Pixbuf, str, str, str, int)
        self.ct_tv, self.ct_filter, sw = self._tree(self.ct_store, False)
        self.ct_q = ""
        self.ct_filter.set_visible_func(lambda m, it, _: not self.ct_q or self.ct_q in (m[it][1] + m[it][2]).lower())
        self.ct_tv.connect("row-activated", lambda tv, path, _c: self.dial(tv.get_model()[path][2]))
        self.ct_tv.connect("button-press-event", self._contact_menu)
        page.pack_start(sw, True, True, 0)
        page.pack_start(self._search_row(lambda q: (setattr(self, "ct_q", q), self.ct_filter.refilter()),
                                         lambda: self.edit_contact(None)), False, False, 0)
        return page

    def _statusbar(self):
        bar = css_class(Gtk.Box(spacing=4), "statusbar")
        self.st_icon = Gtk.DrawingArea(width_request=10, height_request=14, valign=Gtk.Align.CENTER, margin_start=5)
        self.st_icon.connect("draw", self._draw_status_icon)
        self.st_label = Gtk.Label(label="Offline", xalign=0)
        self.acc_label = Gtk.Label(label="", margin_end=6)
        ev = Gtk.EventBox()
        ev.add(self.st_label)
        ev.connect("button-press-event", lambda *_: self.toggle_active(not self.cfg["enabled"]) if
                   self.reg_state in ("offline", "error") or not self.cfg["enabled"] else None)
        bar.pack_start(self.st_icon, False, False, 0)
        bar.pack_start(ev, True, True, 2)
        bar.pack_end(self.acc_label, False, False, 0)
        bar.pack_end(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)
        for w in (bar,):
            w.set_size_request(-1, 22)
        return bar

    def _draw_status_icon(self, w, cr):
        col = {"online": (0.13, 0.65, 0.20), "registering": (0.95, 0.65, 0.10),
               "error": (0.85, 0.15, 0.15)}.get(self.reg_state, (0.6, 0.6, 0.6))
        r, x, y, wd, h = 2, 0.5, 0.5, 9, 13
        cr.new_sub_path()
        cr.arc(x + wd - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + wd - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
        cr.close_path()
        cr.set_source_rgb(*col)
        cr.fill_preserve()
        cr.set_source_rgb(col[0] * 0.6, col[1] * 0.6, col[2] * 0.6)
        cr.set_line_width(1)
        cr.stroke()
        cr.set_source_rgba(1, 1, 1, 0.75)
        cr.rectangle(2.5, 2.5, 5, 4)
        cr.fill()

    def _menu_item(self, menu, label, cb, accel=None, check=None, sensitive=True):
        if check is None:
            it = Gtk.MenuItem(label=label)
        else:
            it = Gtk.CheckMenuItem(label=label)
            it.set_active(check)
        it.set_sensitive(sensitive)
        it.connect("activate", lambda w: cb(w))
        if accel:
            key, mods = Gtk.accelerator_parse(accel)
            it.add_accelerator("activate", self.accel, key, mods, Gtk.AccelFlags.VISIBLE)
        menu.append(it)
        return it

    def _build_menu(self):
        m = Gtk.Menu()
        if len(self.cfg["accounts"]) > 1:
            group = None
            for i, a in enumerate(self.cfg["accounts"]):
                it = Gtk.RadioMenuItem.new_with_label_from_widget(group, a.get("name") or a.get("user"))
                group = group or it
                it.set_active(i == self.cfg["active"])
                it.connect("toggled", lambda w, i=i: w.get_active() and self.switch_account(i))
                m.append(it)
            m.append(Gtk.SeparatorMenuItem())
        self.mi_active = self._menu_item(m, "Tornar Ativo", lambda w: self.toggle_active(w.get_active()),
                                         check=bool(self.cfg["enabled"]))
        self._menu_item(m, "Editar Conta", lambda w: self.edit_account(), "<Control>m")
        self._menu_item(m, "Adicionar Conta...", lambda w: self.add_account())
        self._menu_item(m, "Configurações", lambda w: self.open_settings(), "<Control>p")
        m.append(Gtk.SeparatorMenuItem())
        self._menu_item(m, "Atalhos", lambda w: self.show_shortcuts(), "<Control>s")
        acc = self.account()
        if acc and acc.get("voicemail"):
            self._menu_item(m, "Correio de Voz", lambda w: self.dial(acc["voicemail"]))
        m.append(Gtk.SeparatorMenuItem())
        self._menu_item(m, "Sempre no topo", lambda w: self._on_top(w.get_active()), check=self.s["always_on_top"])
        self._menu_item(m, "Ver Arquivo de Log", lambda w: self._open(LOG_FILE))
        m.append(Gtk.SeparatorMenuItem())
        self._menu_item(m, "Visitar Website", lambda w: self._open(REPO_URL), "<Control>w")
        self._menu_item(m, "Verificar Atualizações", lambda w: self.check_updates(manual=True), "<Control>u")
        m.append(Gtk.SeparatorMenuItem())
        help_it = Gtk.MenuItem()
        hb = Gtk.Box(spacing=40)
        hb.pack_start(Gtk.Label(label="Ajuda", xalign=0), True, True, 0)
        hb.pack_end(Gtk.Label(label=f"Ver. {VERSION}"), False, False, 0)
        help_it.add(hb)
        help_it.connect("activate", lambda w: self.show_about())
        m.append(help_it)
        m.append(Gtk.SeparatorMenuItem())
        self._menu_item(m, "Sair", lambda w: self.quit(), "<Control>q")
        m.show_all()
        return m

    def rebuild_menu(self):
        old = self.menu
        self.menu = self._build_menu()
        self.nb.get_action_widget(Gtk.PackType.END).set_popup(self.menu)
        old.destroy()

    # ---- conta / motor

    def account(self):
        accs = self.cfg["accounts"]
        if not accs:
            return None
        self.cfg["active"] = min(self.cfg["active"], len(accs) - 1)
        return accs[self.cfg["active"]]

    def save_cfg(self):
        save_json(CFG_FILE, self.cfg)

    def start_engine(self):
        acc = self.account()
        if not acc or not self.cfg["enabled"]:
            threading.Thread(target=self.engine.stop, daemon=True).start()
            self.on_reg_state("offline", "Offline")
            return
        self._apply_audio()

        def run():
            self.engine.start(acc, self.s)
        threading.Thread(target=run, daemon=True).start()

    def _first_run(self):
        acc = dict(DEFAULT_ACCOUNT)
        d = AccountDialog(self, acc, False)
        if d.run() == Gtk.ResponseType.OK:
            self.cfg["accounts"] = [d.acc]
            self.cfg["active"] = 0
            self.save_cfg()
            self.rebuild_menu()
            self.update_title()
            self.start_engine()
        d.destroy()

    def edit_account(self):
        acc = self.account()
        if not acc:
            return self._first_run()
        d = AccountDialog(self, acc, True)
        if d.run() == Gtk.ResponseType.OK:
            if d.removed:
                del self.cfg["accounts"][self.cfg["active"]]
                self.cfg["active"] = 0
            else:
                self.cfg["accounts"][self.cfg["active"]] = d.acc
            self.save_cfg()
            self.rebuild_menu()
            self.update_title()
            self.start_engine()
        d.destroy()

    def add_account(self):
        d = AccountDialog(self, {}, False)
        if d.run() == Gtk.ResponseType.OK:
            self.cfg["accounts"].append(d.acc)
            self.cfg["active"] = len(self.cfg["accounts"]) - 1
            self.save_cfg()
            self.rebuild_menu()
            self.update_title()
            self.start_engine()
        d.destroy()

    def switch_account(self, i):
        if i == self.cfg["active"]:
            return
        self.cfg["active"] = i
        self.save_cfg()
        self.update_title()
        self.start_engine()

    def toggle_active(self, on):
        self.cfg["enabled"] = bool(on)
        self.save_cfg()
        self.mi_active.set_active(bool(on))
        self.start_engine()

    def open_settings(self):
        d = SettingsDialog(self, self.s)
        if d.run() == Gtk.ResponseType.OK:
            old_port = self.s["sip_port"]
            self.s.update(d.result())
            self.save_cfg()
            self.sounds.device = self.s["ring_device"] or None
            self._apply_audio()
            self.engine.cfg.update(self.s)
            if old_port != self.s["sip_port"]:
                self.start_engine()
        d.destroy()

    def _apply_audio(self):
        hub = self.engine.hub
        hub.mic_dev = self.s["mic"] or None
        hub.spk_dev = self.s["speaker"] or None
        hub.mic_gain = self.s["mic_vol"] / 100.0
        hub.spk_gain = self.s["spk_vol"] / 100.0
        hub.conference = bool(self.s["conf"])
        self.engine.cfg.update(self.s)

    def _set_volume(self, key, v):
        self.s[key] = int(v)
        self._apply_audio()
        GLib.timeout_add(800, self._save_later)

    def _save_later(self):
        self.save_cfg()
        return False

    def _on_toggle(self, key, on):
        self.s[key] = on
        self.save_cfg()
        self._apply_audio()
        if key == "rec":
            for c in self.active_calls():
                if c.state in ("confirmed", "hold"):
                    self.set_call_recording(c, on)

    def _on_top(self, on):
        self.s["always_on_top"] = on
        self.set_keep_above(on)
        self.save_cfg()

    def update_title(self):
        acc = self.account()
        name = (acc.get("name") or acc.get("user")) if acc else ""
        self.set_title(f"{APP_NAME} - {name}" if name else APP_NAME)
        self.acc_label.set_text(name)

    # ---- callbacks do motor (já na thread GTK)

    def on_reg_state(self, state, text):
        self.reg_state = state
        self.st_label.set_text(text if self.cfg["enabled"] or state != "offline" else "Offline")
        self.st_icon.queue_draw()

    def on_incoming(self, call):
        name = self.lookup_name(call.number, call.display)
        view = self._add_view(call, focus=False)
        if self.s["aa"] or call.auto_answer:
            GLib.timeout_add(500, lambda: (self.answer_call(call), False)[1])
            return
        if self.s["ring_enabled"]:
            self.sounds.loop("ring")
        self.popups[call.id] = IncomingPopup(self, call, name)
        self._notify("Chamada recebida", name if name == call.number else f"{name} ({call.number})")
        view.update()

    def on_call_update(self, call):
        view = self.views.get(call.id)
        if not view and call.state != "ended":
            view = self._add_view(call, focus=True)
        if view:
            view.update()
        if call.direction == "out":
            if call.state == "ringing" and not call.early_media:
                self.sounds.loop("ringback")
            else:
                self.sounds.stop("ringback")
        if call.state != "incoming":
            p = self.popups.pop(call.id, None)
            if p:
                p.destroy()
            if not any(c.state == "incoming" for c in self.active_calls()):
                self.sounds.stop("ring")
        if call.state in ("confirmed",) and self.s["rec"] and call.rtp and not call.rtp.recorder:
            self.set_call_recording(call, True)
        if call.state == "ended" and not getattr(call, "_logged", False):
            call._logged = True
            self._log_call(call)
            if call.direction == "out" and not call.answered and call.reason[:1] in ("4", "5", "6") \
                    and not call.reason.startswith("487"):
                self.sounds.play("busy")
            GLib.timeout_add(1500 if call.answered or call.direction == "out" else 0,
                             lambda: (self.close_call_tab(call), False)[1])
        self.update_call_button()

    def on_transfer_result(self, call, ok, text):
        if not ok:
            self._msg(f"Falha na transferência: {text}")

    def on_message(self, display, user, text):
        w = self.open_message(user)
        if w:
            w.add_line(display or self.lookup_name(user), text, "them")
            self.sounds.play("message")

    def on_message_status(self, number, ok, text):
        w = self.msg_windows.get(number)
        if w and not ok:
            w.info(f"Não entregue: {text}")

    # ---- chamadas

    def active_calls(self):
        return [v.call for v in self.views.values() if not v.call.ended]

    def current_call(self):
        page = self.nb.get_nth_page(self.nb.get_current_page())
        if isinstance(page, CallView) and not page.call.ended:
            return page.call
        live = [c for c in self.active_calls() if c.state in ("confirmed", "calling", "ringing")]
        return live[-1] if live else None

    def _add_view(self, call, focus):
        v = CallView(self, call)
        self.views[call.id] = v
        n = self.nb.append_page(v, v.tab)
        if focus:
            self.nb.set_current_page(n)
        return v

    def close_call_tab(self, call):
        v = self.views.pop(call.id, None)
        if v:
            n = self.nb.page_num(v)
            if n >= 0:
                self.nb.remove_page(n)
        self.update_call_button()

    def dial(self, number):
        number = (number or "").strip()
        if not number:
            return
        if self.reg_state != "online" and not self.engine.running:
            self._msg("A conta não está registrada. Verifique Editar Conta.")
            return
        for c in self.active_calls():
            if c.state == "confirmed":
                self.engine.set_hold(c, True)
        self.engine.make_call(number)
        h = [x for x in self.s["history"] if x != number]
        self.s["history"] = [number] + h[:29]
        self.save_cfg()
        self.refresh_history()
        self.entry.set_text("")

    def answer_call(self, call):
        for c in self.active_calls():
            if c is not call and c.state == "confirmed":
                self.engine.set_hold(c, True)
        self.engine.answer(call)
        v = self.views.get(call.id)
        if v:
            self.nb.set_current_page(self.nb.page_num(v))
        self.present()

    def hangup_call(self, call):
        self.engine.hangup(call)

    def dtmf(self, call, digit):
        if self.s["keypad_sound"]:
            self.sounds.dtmf(digit)
        if call:
            self.engine.send_dtmf(call, digit)

    def transfer_dialog(self, call):
        r = ask_text(self, "Transferir", [("Transferir para", "")])
        if r and r[0]:
            self.engine.transfer(call, r[0])

    def set_call_recording(self, call, on):
        path = None
        if on:
            stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            safe = "".join(ch for ch in call.number if ch.isalnum() or ch in "+-_")
            path = os.path.join(self.s["rec_dir"], f"{stamp}_{call.direction}_{safe}.wav")
        self.engine.set_recording(call, path)
        v = self.views.get(call.id)
        if v:
            v.update()

    def on_call_button(self):
        num = self.entry.get_text().strip()
        if num:
            self.dial(num)
            return
        c = self.current_call()
        if c:
            self.hangup_call(c)

    def update_call_button(self):
        if not hasattr(self, "call_btn"):
            return
        ctx = self.call_btn.get_style_context()
        if not self.entry.get_text().strip() and self.current_call():
            self.call_btn.set_label("Desligar")
            ctx.add_class("hangup")
        else:
            self.call_btn.set_label("Chamar")
            ctx.remove_class("hangup")

    def on_key_digit(self, d):
        c = self.current_call()
        if c and c.state == "confirmed":
            self.dtmf(c, d)
            return
        if self.s["keypad_sound"]:
            self.sounds.dtmf(d)
        self._insert(d)

    def _insert(self, text):
        pos = self.entry.get_position()
        self.entry.insert_text(text, pos)
        self.entry.set_position(pos + len(text))

    def redial(self):
        if self.s["history"]:
            self.dial(self.s["history"][0])

    def _tick(self):
        for v in self.views.values():
            v.tick()
        return True

    # ---- registros e contatos

    def lookup_name(self, number, display=""):
        for c in self.contacts:
            if c["number"] == number:
                return c["name"]
        return display or number

    def _log_call(self, call):
        if call.direction == "in":
            kind = "in" if call.answered else "missed"
        else:
            kind = "out"
        dur = int(call.ended - call.answered) if call.answered else 0
        self.calls_log.insert(0, {"type": kind, "number": call.number, "name": call.display,
                                  "time": call.created, "duration": dur, "reason": call.reason})
        del self.calls_log[500:]
        save_json(CALLS_FILE, self.calls_log)
        self.refresh_log()

    def refresh_log(self):
        self.log_store.clear()
        for i, e in enumerate(self.calls_log):
            t = datetime.datetime.fromtimestamp(e["time"]).strftime("%d/%m/%Y %H:%M")
            d = e.get("duration", 0)
            tip = f"{t}  •  {d // 60:02d}:{d % 60:02d}" if d else t
            if e.get("type") == "missed":
                tip += "  •  perdida"
            self.log_store.append([self.icons.get(e["type"], self.icons["out"]),
                                   self.lookup_name(e["number"], e.get("name", "")), e["number"],
                                   GLib.markup_escape_text(tip), i])

    def refresh_contacts(self):
        self.ct_store.clear()
        for i, c in sorted(enumerate(self.contacts), key=lambda x: x[1]["name"].lower()):
            self.ct_store.append([None, c["name"], c["number"], GLib.markup_escape_text(c["number"]), i])

    def refresh_history(self):
        self.combo.remove_all()
        for n in self.s["history"]:
            self.combo.append_text(n)

    def _popup(self, items, event):
        m = Gtk.Menu()
        for lab, cb in items:
            if lab is None:
                m.append(Gtk.SeparatorMenuItem())
                continue
            it = Gtk.MenuItem(label=lab)
            it.connect("activate", lambda _w, cb=cb: cb())
            m.append(it)
        m.show_all()
        m.attach_to_widget(self, None)
        m.popup_at_pointer(event)

    def _row_at(self, tv, event):
        r = tv.get_path_at_pos(int(event.x), int(event.y))
        if not r:
            return None
        tv.get_selection().select_path(r[0])
        return tv.get_model()[r[0]]

    def _log_menu(self, tv, event):
        if event.button != 3:
            return False
        row = self._row_at(tv, event)
        if not row:
            items = [("Limpar registros", self.clear_log)]
        else:
            num, idx, name = row[2], row[4], row[1]
            items = [("Chamar", lambda: self.dial(num)), ("Mensagem", lambda: self.open_message(num)),
                     ("Copiar número", lambda: self._copy(num)),
                     ("Adicionar aos contatos", lambda: self.edit_contact(None, num, name if name != num else "")),
                     (None, None), ("Excluir", lambda: self.delete_log(idx)), ("Limpar registros", self.clear_log)]
        self._popup(items, event)
        return True

    def _contact_menu(self, tv, event):
        if event.button != 3:
            return False
        row = self._row_at(tv, event)
        items = [("Adicionar contato", lambda: self.edit_contact(None))]
        if row:
            num, idx = row[2], row[4]
            items = [("Chamar", lambda: self.dial(num)), ("Mensagem", lambda: self.open_message(num)),
                     ("Copiar número", lambda: self._copy(num)), (None, None),
                     ("Editar", lambda: self.edit_contact(idx)), ("Excluir", lambda: self.delete_contact(idx)),
                     (None, None)] + items
        self._popup(items, event)
        return True

    def delete_log(self, idx):
        del self.calls_log[idx]
        save_json(CALLS_FILE, self.calls_log)
        self.refresh_log()

    def clear_log(self):
        self.calls_log = []
        save_json(CALLS_FILE, self.calls_log)
        self.refresh_log()

    def edit_contact(self, idx, number="", name=""):
        c = self.contacts[idx] if idx is not None else {"name": name, "number": number}
        r = ask_text(self, "Contato", [("Nome", c["name"]), ("Número", c["number"])])
        if not r or not r[1]:
            return
        new = {"name": r[0] or r[1], "number": r[1]}
        if idx is None:
            self.contacts.append(new)
        else:
            self.contacts[idx] = new
        save_json(CONTACTS_FILE, self.contacts)
        self.refresh_contacts()
        self.refresh_log()

    def delete_contact(self, idx):
        del self.contacts[idx]
        save_json(CONTACTS_FILE, self.contacts)
        self.refresh_contacts()
        self.refresh_log()

    def open_message(self, number):
        if not number:
            self._msg("Digite um número para enviar mensagem.")
            return None
        w = self.msg_windows.get(number)
        if not w:
            w = self.msg_windows[number] = MessageWindow(self, number)
        w.present()
        return w

    # ---- atualização

    def check_updates(self, manual):
        def work():
            try:
                remote = updater.latest_version(REPO, BRANCH)
                err = None
            except Exception as e:
                remote, err = None, str(e)
            GLib.idle_add(lambda: (self._update_checked(remote, err, manual), False)[1])
        threading.Thread(target=work, daemon=True).start()

    def _update_checked(self, remote, err, manual):
        self.s["last_update_check"] = time.time()
        self.save_cfg()
        if err:
            log.warning("verificação de atualização falhou: %s", err)
            if manual:
                self._msg(f"Não foi possível verificar atualizações.\n\n{err}")
            return
        if not updater.is_newer(remote, VERSION):
            if manual:
                self._msg(f"Você já está usando a versão mais recente ({VERSION}).")
            return
        if not updater.writable(HERE):
            self._msg(f"Nova versão {remote} disponível, mas a pasta {HERE} não tem permissão de escrita.\n"
                      f"Baixe em {REPO_URL}")
            return
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.YES_NO,
                              text=f"Nova versão disponível: {remote}\n(versão atual: {VERSION})\n\nAtualizar agora?")
        r = d.run()
        d.destroy()
        if r != Gtk.ResponseType.YES:
            return

        def work():
            try:
                updater.update(REPO, BRANCH, HERE)
                err = None
            except Exception as e:
                err = str(e)
            GLib.idle_add(lambda: (self._update_done(remote, err), False)[1])
        threading.Thread(target=work, daemon=True).start()

    def _update_done(self, remote, err):
        if err:
            log.error("atualização falhou: %s", err)
            self._msg(f"A atualização falhou:\n\n{err}")
            return
        log.info("atualizado para %s", remote)
        if self.active_calls():
            self._msg(f"Atualizado para a versão {remote}. A nova versão será usada quando o programa for reaberto.")
            return
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.INFO,
                              buttons=Gtk.ButtonsType.OK, text=f"Atualizado para a versão {remote}.\nO programa será reiniciado.")
        d.run()
        d.destroy()
        script = os.path.join(HERE, "micruxsip.py")
        subprocess.Popen(["sh", "-c", 'sleep 2; exec "$0" "$1"', sys.executable, script], start_new_session=True)
        self.quit()

    # ---- diversos

    def _copy(self, text):
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)

    def _open(self, target):
        subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _notify(self, title, body):
        if shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-a", APP_NAME, "-u", "critical", title, body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _msg(self, text):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.INFO,
                              buttons=Gtk.ButtonsType.OK, text=text)
        d.run()
        d.destroy()

    def show_shortcuts(self):
        self._msg("Atalhos\n\n"
                  "Ctrl+M\tEditar conta\nCtrl+P\tConfigurações\nCtrl+S\tAtalhos\nCtrl+W\tVisitar website\n"
                  "Ctrl+U\tVerificar atualizações\n"
                  "Ctrl+Q\tSair\nEnter\tChamar\nEsc\tDesligar / recusar\n0-9 * #\tDiscar / DTMF\n"
                  "F2\tAtender chamada recebida")

    def show_about(self):
        d = Gtk.AboutDialog(transient_for=self, modal=True, program_name=APP_NAME, website=REPO_URL, license_type=Gtk.License.MIT_X11,
                            version=VERSION, logo=app_icon(96),
                            comments="Softphone SIP (UDP, G.711, DTMF RFC 2833/INFO, espera, transferência, "
                                     "gravação, conferência, mensagens).")
        d.run()
        d.destroy()

    def _on_key(self, w, ev):
        name = Gdk.keyval_name(ev.keyval) or ""
        if ev.state & Gdk.ModifierType.CONTROL_MASK:
            return False
        if name == "Escape":
            c = self.current_call()
            ring = [x for x in self.active_calls() if x.state == "incoming"]
            if ring:
                self.hangup_call(ring[-1])
            elif c:
                self.hangup_call(c)
            return True
        if name == "F2":
            ring = [x for x in self.active_calls() if x.state == "incoming"]
            if ring:
                self.answer_call(ring[-1])
            return True
        ch = Gdk.keyval_to_unicode(ev.keyval)
        ch = chr(ch) if ch else ""
        page = self.nb.get_nth_page(self.nb.get_current_page())
        if ch and ch in "0123456789*#":
            if isinstance(page, CallView):
                self.dtmf(page.call, ch)
                return True
            if not self.entry.has_focus() and self.nb.get_current_page() == 0:
                self.on_key_digit(ch)
                return True
        return False

    def _on_close(self, *_):
        if self.active_calls():
            d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                  buttons=Gtk.ButtonsType.YES_NO, text="Há chamadas em andamento. Sair mesmo assim?")
            r = d.run()
            d.destroy()
            if r != Gtk.ResponseType.YES:
                return True
        self.quit()
        return True

    def quit(self):
        for s in list(self.sounds.loops):
            self.sounds.stop(s)
        self.save_cfg()
        self.hide()
        try:
            self.engine.stop()
        except Exception:
            log.exception("erro ao parar")
        self.app.quit()


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="io.github.ezequieljc.MicruxSip",
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.win = None

    def do_command_line(self, cmd):
        args = cmd.get_arguments()[1:]
        if not self.win:
            self.win = MainWindow(self)
        self.win.present()
        if args:
            num = args[0]
            if num.startswith(("sip:", "tel:", "callto:")):
                num = sipcore.uri_user(num.replace("callto:", "sip:", 1)) if "@" not in num else num
            GLib.idle_add(lambda: (self.win.dial(num), False)[1])
        return 0


def main():
    os.makedirs(APP_DIR, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=1, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.DEBUG if os.environ.get("MICROSIP_DEBUG") else logging.INFO, handlers=[h])
    logging.getLogger("sip").setLevel(logging.DEBUG)
    GLib.set_prgname("micruxsip")
    GLib.set_application_name(APP_NAME)
    settings = Gtk.Settings.get_default()
    settings.set_property("gtk-theme-name", "Adwaita")
    settings.set_property("gtk-application-prefer-dark-theme", False)
    settings.set_property("gtk-icon-theme-name", "Adwaita")
    make_sounds()
    sys.exit(App().run(sys.argv))


if __name__ == "__main__":
    main()
