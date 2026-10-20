#!/bin/bash
# Instala el asistente: entorno de Python, modelos de voz a texto (Vosk y Whisper), voz de Piper,
# lanzador en el menú y (opcional) inicio automático. Se puede volver a ejecutar sin problema.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

MODELO_URL="https://alphacephei.com/vosk/models/vosk-model-small-es-0.42.zip"
VOZ="${VOZ:-es_MX-claude-high}"
VOZ_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main/es/$(echo "$VOZ" | awk -F- '{print $1"/"$2"/"$3}')/$VOZ"

verde() { printf '\033[32m%s\033[0m\n' "$*"; }
aviso() { printf '\033[33m%s\033[0m\n' "$*"; }
error() { printf '\033[31m%s\033[0m\n' "$*"; exit 1; }

echo "== Comprobando requisitos =="
faltan=()
for cmd in python3 parecord pacat paplay curl unzip; do
    command -v "$cmd" >/dev/null || faltan+=("$cmd")
done
if ((${#faltan[@]})); then
    error "Faltan programas: ${faltan[*]}
  Fedora/Bazzite: sudo dnf install pulseaudio-utils unzip   (o rpm-ostree install en sistemas inmutables)
  Debian/Ubuntu:  sudo apt install pulseaudio-utils unzip python3-venv
  Arch:           sudo pacman -S libpulse unzip"
fi
command -v claude >/dev/null || aviso "⚠ No se encontró 'claude' (Claude Code). Instálalo e inicia sesión antes de usar el asistente:
  curl -fsSL https://claude.ai/install.sh | bash   y luego ejecuta: claude"
command -v espeak-ng >/dev/null || command -v espeak >/dev/null || \
    aviso "⚠ No se encontró espeak-ng. Piper lo incluye, así que no es imprescindible."

echo "== Entorno de Python =="
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt
verde "Dependencias instaladas"

echo "== Modelo de reconocimiento de voz (español, ~40 MB) =="
if [ ! -d modelo ]; then
    curl -fSL --progress-bar -o modelo.zip "$MODELO_URL"
    unzip -q modelo.zip && rm modelo.zip
    mv vosk-model-small-es-* modelo
fi
verde "Modelo listo"

WHISPER="$(./venv/bin/python -c 'import json,os; c=json.load(open("config.json")) if os.path.exists("config.json") else {}; print(c.get("whisper", "small"))')"
if [ -n "$WHISPER" ]; then
    echo "== Modelo de Whisper: $WHISPER (small ~460 MB) =="
    ./venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('$WHISPER', device='cpu', compute_type='int8')"
    verde "Whisper listo"
fi

echo "== Voz de Piper: $VOZ (~60 MB) =="
mkdir -p voces
if [ ! -f "voces/$VOZ.onnx" ]; then
    curl -fSL --progress-bar -o "voces/$VOZ.onnx" "$VOZ_URL.onnx"
    curl -fsSL -o "voces/$VOZ.onnx.json" "$VOZ_URL.onnx.json"
fi
verde "Voz lista"

[ -f config.json ] || cp config.example.json config.json
if [ "$VOZ" != "es_MX-claude-high" ]; then
    ./venv/bin/python - "$VOZ" <<'EOF'
import json, sys
c = json.load(open("config.json")); c["voz_piper"] = sys.argv[1]
json.dump(c, open("config.json", "w"), indent=2, ensure_ascii=False)
EOF
fi

echo "== Lanzador =="
mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/asistente-claude.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Asistente Claude (hablar)
Comment=Pulsa para hablarle a Claude; pulsa otra vez para cancelar
Exec=$DIR/venv/bin/python $DIR/asistente.py
Icon=audio-input-microphone
Terminal=false
Categories=Utility;
EOF
update-desktop-database ~/.local/share/applications 2>/dev/null || true
verde "Lanzador «Asistente Claude (hablar)» creado en el menú de aplicaciones"

read -rp "¿Quieres que la esfera se abra sola al iniciar sesión? [S/n] " r
if [[ ! "$r" =~ ^[nN]$ ]]; then
    ./activar.sh
else
    (setsid "$DIR/venv/bin/python" "$DIR/orbe.py" >/dev/null 2>>"$DIR/asistente.log" </dev/null &)
fi

echo
verde "¡Instalación terminada!"
echo "Último paso: asigna un atajo de teclado al lanzador «Asistente Claude (hablar)»."
echo "  KDE:   Preferencias del sistema → Teclado → Accesos rápidos → Añadir nuevo → Aplicación"
echo "  GNOME: Configuración → Teclado → Atajos personalizados → comando: $DIR/venv/bin/python $DIR/asistente.py"
echo "También puedes simplemente hacer clic en la esfera."
