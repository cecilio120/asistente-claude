#!/bin/bash
# Elimina el asistente por completo: lo cierra, quita el inicio automático y el
# lanzador del menú, y borra la carpeta con todo (modelos, voz, historial).
# El atajo de teclado hay que quitarlo a mano en Preferencias del sistema → Teclado → Accesos rápidos.
DIR="$(cd "$(dirname "$0")" && pwd)"
read -rp "¿Seguro que quieres borrar el asistente y la carpeta $DIR? [s/N] " r
[[ "$r" =~ ^[sS]$ ]] || { echo "Cancelado."; exit 0; }
"$DIR/venv/bin/python" "$DIR/asistente.py" salir 2>/dev/null
sleep 1
rm -f ~/.config/autostart/asistente-claude.desktop ~/.local/share/applications/asistente-claude.desktop
update-desktop-database ~/.local/share/applications 2>/dev/null
rm -rf "$DIR"
echo "Asistente eliminado. Tu historial de conversaciones de Claude Code no se ha tocado."
