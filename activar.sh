#!/bin/bash
# Vuelve a poner la esfera en el inicio automático y la abre.
DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p ~/.config/autostart
cat > ~/.config/autostart/asistente-claude.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Asistente Claude (esfera)
Comment=Esfera flotante del asistente de voz de Claude
Exec=$DIR/venv/bin/python $DIR/orbe.py
Icon=audio-input-microphone
Terminal=false
X-KDE-autostart-phase=2
EOF
(setsid "$DIR/venv/bin/python" "$DIR/orbe.py" >/dev/null 2>>"$DIR/asistente.log" </dev/null &)
echo "Asistente activado."
