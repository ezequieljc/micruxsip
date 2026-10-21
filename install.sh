#!/bin/sh
# Instala o MicruxSip para o usuário atual (sem precisar de root, exceto para as dependências).
#
#   ./install.sh            instala dependências + programa
#   ./install.sh --no-deps  só copia o programa
#
# Também funciona direto da internet:
#   wget -qO- https://raw.githubusercontent.com/ezequieljc/micruxsip/main/install.sh | sh
set -e

REPO="ezequieljc/micruxsip"
BRANCH="main"
DEST="$HOME/.local/share/micruxsip"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/128x128/apps"
DEPS="python3 python3-gi python3-gi-cairo gir1.2-gtk-3.0 pulseaudio-utils libnotify-bin xdg-utils"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }

install_deps() {
    missing=""
    for p in $DEPS; do
        dpkg -s "$p" >/dev/null 2>&1 || missing="$missing $p"
    done
    [ -z "$missing" ] && { info "Dependências já instaladas."; return; }
    info "Instalando dependências:$missing"
    if [ "$(id -u)" = 0 ]; then
        apt-get update && apt-get install -y $missing
    elif command -v sudo >/dev/null 2>&1 && sudo -v 2>/dev/null; then
        sudo apt-get update && sudo apt-get install -y $missing
    else
        warn "Seu usuário não tem sudo. Digite a senha do root:"
        su -c "apt-get update && apt-get install -y $missing"
    fi
}

[ "$1" = "--no-deps" ] || install_deps

SRC="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
TMP=""
if [ ! -f "$SRC/micruxsip.py" ]; then
    info "Baixando o MicruxSip do GitHub..."
    TMP="$(mktemp -d)"
    python3 -c "import urllib.request,sys; open(sys.argv[2],'wb').write(urllib.request.urlopen(sys.argv[1]).read())" \
        "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH" "$TMP/src.tar.gz"
    tar -xzf "$TMP/src.tar.gz" -C "$TMP"
    SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n1)"
fi

info "Copiando arquivos para $DEST"
mkdir -p "$DEST" "$BIN" "$APPS" "$ICONS"
for f in micruxsip.py sipcore.py updater.py VERSION README.md LICENSE install.sh uninstall.sh; do
    [ -f "$SRC/$f" ] && cp "$SRC/$f" "$DEST/"
done
cp -r "$SRC/data" "$DEST/"
chmod +x "$DEST/micruxsip.py" "$DEST/install.sh" "$DEST/uninstall.sh"
cp "$SRC/data/micruxsip.png" "$ICONS/micruxsip.png"

cat > "$BIN/micruxsip" <<EOF
#!/bin/sh
exec python3 "$DEST/micruxsip.py" "\$@"
EOF
chmod +x "$BIN/micruxsip"

cat > "$APPS/micruxsip.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=MicruxSip
GenericName=Softphone SIP
Comment=Faça e receba ligações pelo seu ramal SIP
Exec=$BIN/micruxsip %u
Icon=micruxsip
Terminal=false
Categories=Network;Telephony;
Keywords=sip;voip;telefone;ramal;softphone;
MimeType=x-scheme-handler/sip;x-scheme-handler/tel;x-scheme-handler/callto;
StartupWMClass=micruxsip
EOF

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
[ -n "$TMP" ] && rm -rf "$TMP"

info "Pronto! Abra o MicruxSip pelo menu de aplicativos (Internet) ou rode: micruxsip"
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) warn "$BIN ainda não está no PATH; ele entra depois de sair e entrar na sessão." ;;
esac
