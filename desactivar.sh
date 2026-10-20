#!/bin/bash
# Cierra la esfera y la quita del inicio automático (no borra nada más).
# Para volver a activarla: ./activar.sh
cd "$(dirname "$0")"
./venv/bin/python asistente.py salir 2>/dev/null
rm -f ~/.config/autostart/asistente-claude.desktop
echo "Asistente desactivado: cerrado y fuera del inicio automático."
