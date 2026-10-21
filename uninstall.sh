#!/bin/sh
# Remove o MicruxSip. Use --purge para apagar também contas, contatos e histórico.
set -e
rm -rf "$HOME/.local/share/micruxsip"
rm -f "$HOME/.local/bin/micruxsip" \
      "$HOME/.local/share/applications/micruxsip.desktop" \
      "$HOME/.local/share/icons/hicolor/128x128/apps/micruxsip.png"
if [ "$1" = "--purge" ]; then
    rm -rf "$HOME/.config/micruxsip"
    echo "MicruxSip e configurações removidos."
else
    echo "MicruxSip removido. Suas configurações continuam em ~/.config/micruxsip (use --purge para apagar)."
fi
