// Lo carga orbe.py en KWin: avisa a la esfera cuando la ventana activa está a
// pantalla completa (juegos, vídeos) para que se oculte.
function revisar() {
    const w = workspace.activeWindow;
    callDBus("org.claude.Asistente", "/", "org.claude.Asistente", "pantallaCompleta",
             !!(w && w.fullScreen));
}

function vigilar(w) {
    w.fullScreenChanged.connect(revisar);
}

workspace.windowList().forEach(vigilar);
workspace.windowAdded.connect(vigilar);
workspace.windowActivated.connect(revisar);
workspace.windowRemoved.connect(revisar);
revisar();
