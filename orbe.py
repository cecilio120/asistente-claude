#!/usr/bin/env python3
"""Esfera flotante del asistente: un holograma que duerme en el escritorio y se
ilumina al hablar con él.

Clic: hablar (o cancelar si está ocupado). Arrastrar: moverla. Clic derecho: menú.
El atajo de teclado (asistente.py) le manda órdenes por un socket local.
"""
import json
import math
import os
import random
import sys
import threading
import time

# XWayland: en Wayland puro una ventana no puede elegir su posición ni quedarse encima.
os.environ["QT_QPA_PLATFORM"] = "xcb"

from PyQt6.QtCore import QObject, QPointF, QRect, QRectF, Qt, QTimer, pyqtClassInfo, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusConnection, QDBusInterface
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QRadialGradient, QRegion
from PyQt6.QtNetwork import QLocalServer
from PyQt6.QtWidgets import QApplication, QMenu, QWidget

import asistente as nucleo

POS_FILE = os.path.join(nucleo.DIR, ".orbe_pos")
SCRIPT_KWIN = os.path.join(nucleo.DIR, "kwin_pantalla_completa.js")
NOMBRE_SCRIPT = "asistente-claude-pantalla-completa"

# Por estado: color, brillo, intensidad de los anillos, velocidad de giro.
ESTADOS = {
    "cargando":    ((60, 110, 190), 0.45, 0.6, 3.0),
    "dormida":     ((40, 100, 185), 0.38, 0.0, 0.15),
    "escuchando":  ((70, 215, 255), 1.00, 0.7, 0.8),
    "pensando":    ((120, 165, 255), 0.90, 1.0, 3.2),
    "hablando":    ((90, 225, 255), 1.00, 0.45, 0.6),
    "confirmando": ((255, 180, 60), 1.00, 0.8, 1.0),
    "error":       ((255, 80, 70), 0.90, 0.6, 0.5),
}
ANCHO, ALTO = 420, 360
RADIO = 46
RETRASO_AUDIO = 0.12  # lo que tarda el audio en salir por los altavoces


def mezclar(a, b, k):
    return a + (b - a) * k


class Puente(QObject):
    """Lleva los avisos del hilo del asistente al hilo de la interfaz."""
    aviso = pyqtSignal(str, dict)


@pyqtClassInfo("D-Bus Interface", "org.claude.Asistente")
class ServicioDBus(QObject):
    """Recibe de KWin si la ventana activa está a pantalla completa."""

    def __init__(self, orbe):
        super().__init__()
        self.orbe = orbe

    @pyqtSlot(bool)
    def pantallaCompleta(self, activa):
        self.orbe.pantalla_completa(activa)


def kwin_scripting():
    return QDBusInterface("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting")


def cargar_script_kwin(orbe):
    bus = QDBusConnection.sessionBus()
    orbe.servicio_dbus = ServicioDBus(orbe)
    if not (bus.registerService("org.claude.Asistente")
            and bus.registerObject("/", orbe.servicio_dbus, QDBusConnection.RegisterOption.ExportAllSlots)):
        nucleo.log("No se pudo registrar el servicio D-Bus; la esfera no se ocultará con juegos")
        return
    kwin = kwin_scripting()
    kwin.call("unloadScript", NOMBRE_SCRIPT)
    kwin.call("loadScript", SCRIPT_KWIN, NOMBRE_SCRIPT)
    kwin.call("start")


class Orbe(QWidget):
    def __init__(self):
        # Sin gestor de ventanas: no sale en la barra de tareas ni en Alt+Tab y está en todos los escritorios.
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool | Qt.WindowType.X11BypassWindowManagerHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Asistente Claude")
        self.escala = nucleo.CONFIG["escala_orbe"]
        self.setFixedSize(int(ANCHO * self.escala), int(ALTO * self.escala))

        self.estado = "cargando"
        color, brillo, anillos, giro = ESTADOS[self.estado]
        self.color = list(color)
        self.brillo, self.anillos, self.giro = brillo, anillos, giro
        self.rotacion = 0.0
        self.energia = 0.0
        self.nivel_mic = 0.0
        self.envolvente, self.fps_env, self.t_habla = [], 60, 0.0
        self.subtitulo, self.t_subtitulo = "", 0.0
        self.alfa_sub = 0.0
        self.t0 = time.time()
        self.particulas = [(random.uniform(0, 2 * math.pi), random.uniform(1.25, 1.85),
                            random.uniform(0.4, 1.2) * random.choice((-1, 1))) for _ in range(14)]
        self.arrastre = None
        self.pendiente_activar = False
        self.oculta_por_juego = False

        self.puente = Puente()
        self.puente.aviso.connect(self.al_aviso)
        self.asistente = None
        threading.Thread(target=self.cargar, daemon=True).start()

        self.servidor = QLocalServer(self)
        QLocalServer.removeServer(nucleo.SOCKET)
        self.servidor.listen(nucleo.SOCKET)
        self.servidor.newConnection.connect(self.al_conectar)

        self.temporizador = QTimer(self)
        self.temporizador.timeout.connect(self.animar)
        self.temporizador.start(16)
        self.colocar()
        self.actualizar_mascara()

    # ---- asistente ----
    def cargar(self):
        self.asistente = nucleo.Asistente(lambda estado, **datos: self.puente.aviso.emit(estado, datos))
        self.puente.aviso.emit("dormida", {})
        if self.pendiente_activar:
            self.puente.aviso.emit("_activar", {})

    def activar(self):
        if not self.asistente:
            self.pendiente_activar = True
        elif self.asistente.ocupado:
            self.asistente.cancelar()
        else:
            threading.Thread(target=self.asistente.conversar, daemon=True).start()

    def nueva_conversacion(self):
        nucleo.borrar_sesion()
        self.mostrar_subtitulo("Conversación nueva")

    def al_conectar(self):
        con = self.servidor.nextPendingConnection()
        con.waitForReadyRead(500)
        orden = bytes(con.readAll()).decode().strip()
        con.close()
        if orden == "activar":
            self.activar()
        elif orden == "nueva":
            self.nueva_conversacion()
        elif orden == "salir":
            QApplication.quit()

    def al_aviso(self, estado, datos):
        if estado == "_activar":
            self.activar()
            return
        if estado == "nivel":
            self.nivel_mic = datos["valor"]
            return
        self.estado = estado
        if self.oculta_por_juego:
            self.pantalla_completa(True)
        if estado == "hablando" or "envolvente" in datos:
            self.envolvente = datos.get("envolvente", [])
            self.fps_env = datos.get("fps", 60)
            self.t_habla = time.time() + RETRASO_AUDIO
        if "texto" in datos:
            self.mostrar_subtitulo(datos["texto"])

    def pantalla_completa(self, activa):
        # Si está ocupada (p. ej. le hablaste con el atajo durante el juego) se muestra igual.
        self.oculta_por_juego = activa
        if activa and self.estado == "dormida":
            self.hide()
        elif not self.isVisible():
            self.show()

    def mostrar_subtitulo(self, texto):
        if not nucleo.CONFIG["subtitulos"]:
            return
        self.subtitulo = texto if len(texto) < 240 else texto[:237] + "…"
        self.t_subtitulo = time.time()

    # ---- animación ----
    def animar(self):
        ahora = time.time()
        t = ahora - self.t0
        color, brillo, anillos, giro = ESTADOS[self.estado]
        k = 0.08
        self.color = [mezclar(c, d, k) for c, d in zip(self.color, color)]
        self.brillo = mezclar(self.brillo, brillo, k)
        self.anillos = mezclar(self.anillos, anillos, k)
        self.giro = mezclar(self.giro, giro, k)
        self.rotacion += self.giro * 0.016

        if self.estado == "escuchando":
            objetivo = self.nivel_mic
        elif self.envolvente and self.estado in ("hablando", "confirmando"):
            i = int((ahora - self.t_habla) * self.fps_env)
            objetivo = self.envolvente[i] if 0 <= i < len(self.envolvente) else 0.0
        elif self.estado == "pensando":
            objetivo = 0.25 + 0.15 * math.sin(t * 6)
        elif self.estado == "dormida":
            objetivo = 0.12 * (0.5 + 0.5 * math.sin(t * 2 * math.pi / 5))  # respiración lenta
        else:
            objetivo = 0.2
        self.energia = mezclar(self.energia, objetivo, 0.35 if objetivo > self.energia else 0.12)

        # El subtítulo se queda mientras está activa y se desvanece unos segundos después.
        visible = bool(self.subtitulo) and (self.estado != "dormida" or ahora - self.t_subtitulo < 6)
        alfa_previo = self.alfa_sub
        self.alfa_sub = mezclar(self.alfa_sub, 1.0 if visible else 0.0, 0.1)
        if self.alfa_sub < 0.01 and not visible:
            self.alfa_sub = 0.0
            self.subtitulo = ""
        if (alfa_previo > 0) != (self.alfa_sub > 0):
            self.actualizar_mascara()
        self.update()

    def centro(self):
        t = time.time() - self.t0
        flotar = math.sin(t * 1.1) * 5 + math.sin(t * 0.47) * 2
        return QPointF(ANCHO / 2, 130 + flotar)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(self.escala, self.escala)
        c = self.centro()
        e, b = self.energia, self.brillo
        r = RADIO * (1 + 0.14 * e)
        col = QColor(*[int(x) for x in self.color])

        def tono(alfa, claro=100):
            q = col.lighter(claro) if claro != 100 else QColor(col)
            q.setAlphaF(max(0.0, min(1.0, alfa)))
            return q

        p.setPen(Qt.PenStyle.NoPen)
        # Halo exterior
        g = QRadialGradient(c, r * 2.6)
        g.setColorAt(0, tono(0.55 * b * (0.7 + 0.6 * e)))
        g.setColorAt(0.35, tono(0.22 * b))
        g.setColorAt(1, tono(0))
        p.setBrush(g)
        p.drawEllipse(c, r * 2.6, r * 2.6)

        # Cuerpo de la esfera, con brillo desplazado para dar volumen
        g = QRadialGradient(c, r, QPointF(c.x() - r * 0.35, c.y() - r * 0.4))
        g.setColorAt(0, tono(0.85 * b, 170))
        g.setColorAt(0.5, tono(0.55 * b))
        g.setColorAt(1, tono(0.75 * b, 60))
        p.setBrush(g)
        p.drawEllipse(c, r, r)

        # Rejilla holográfica: meridianos que giran y paralelos
        p.save()
        recorte = QPainterPath()
        recorte.addEllipse(c, r, r)
        p.setClipPath(recorte)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(tono(0.28 * b, 160), 1))
        for i in range(6):
            fase = self.rotacion * 0.6 + i * math.pi / 6
            p.drawEllipse(c, r * abs(math.cos(fase)), r)
        for k in (-0.66, -0.33, 0.0, 0.33, 0.66):
            mitad = r * math.sqrt(1 - k * k)
            p.drawEllipse(QPointF(c.x(), c.y() + k * r), mitad, mitad * 0.16)
        # Línea de escaneo
        y = c.y() + r * math.sin(time.time() * 1.7)
        p.setPen(QPen(tono(0.35 * b, 180), 1.5))
        p.drawLine(QPointF(c.x() - r, y), QPointF(c.x() + r, y))
        p.restore()

        # Núcleo
        p.setPen(Qt.PenStyle.NoPen)
        rn = r * (0.28 + 0.45 * e)
        g = QRadialGradient(c, rn)
        blanco = QColor(255, 255, 255)
        blanco.setAlphaF(min(1.0, 0.35 + 0.65 * b))
        g.setColorAt(0, blanco)
        g.setColorAt(0.4, tono(0.8 * b, 150))
        g.setColorAt(1, tono(0))
        p.setBrush(g)
        p.drawEllipse(c, rn, rn)

        # Borde
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(tono(0.6 * b, 150), 1.3))
        p.drawEllipse(c, r, r)

        # Anillos segmentados que giran en sentidos opuestos
        if self.anillos > 0.02:
            extra = 12 * self.nivel_mic if self.estado == "escuchando" else 6 * e
            for radio, segmentos, sentido, grosor in ((1.38, 3, 1, 2.6), (1.62, 5, -1, 1.6), (1.85, 8, 1, 1.0)):
                rr = r * radio + extra
                pen = QPen(tono(self.anillos * b * 0.85, 140), grosor)
                pen.setCapStyle(Qt.PenCapStyle.FlatCap)
                p.setPen(pen)
                rect = QRectF(c.x() - rr, c.y() - rr, rr * 2, rr * 2)
                inicio = math.degrees(self.rotacion * sentido * (1 + radio - 1.38))
                hueco = 360 / segmentos
                for s in range(segmentos):
                    p.drawArc(rect, int((inicio + s * hueco) * 16), int(hueco * 0.62 * 16))
            # Partículas en órbita
            p.setPen(Qt.PenStyle.NoPen)
            for angulo, radio, vel in self.particulas:
                a = angulo + self.rotacion * vel
                rr = r * radio + extra
                pos = QPointF(c.x() + math.cos(a) * rr, c.y() + math.sin(a) * rr * 0.92)
                p.setBrush(tono(self.anillos * b * 0.9, 170))
                p.drawEllipse(pos, 1.6, 1.6)

        if self.alfa_sub > 0 and self.subtitulo:
            self.dibujar_subtitulo(p, tono)

    def dibujar_subtitulo(self, p, tono):
        fuente = QFont("Noto Sans", 10)
        p.setFont(fuente)
        caja = QFontMetrics(fuente).boundingRect(QRect(0, 0, 360, 200), Qt.TextFlag.TextWordWrap, self.subtitulo)
        w, h = caja.width() + 28, caja.height() + 16
        rect = QRectF((ANCHO - w) / 2, 250, w, min(h, ALTO - 254))
        a = self.alfa_sub
        fondo = QColor(8, 18, 32)
        fondo.setAlphaF(0.78 * a)
        p.setBrush(fondo)
        p.setPen(QPen(tono(0.55 * a, 130), 1))
        p.drawRoundedRect(rect, 10, 10)
        texto = QColor(225, 245, 255)
        texto.setAlphaF(a)
        p.setPen(texto)
        p.drawText(rect.adjusted(14, 8, -14, -8), Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignHCenter,
                   self.subtitulo)

    def actualizar_mascara(self):
        """Solo la esfera (y el subtítulo si se ve) reciben clics; el resto deja pasar el ratón."""
        s = self.escala
        rr = int(RADIO * 2.4 * s)
        cx, cy = int(ANCHO / 2 * s), int(130 * s)
        region = QRegion(QRect(cx - rr, cy - rr, rr * 2, rr * 2), QRegion.RegionType.Ellipse)
        if self.alfa_sub > 0:
            region = region.united(QRegion(QRect(0, int(245 * s), self.width(), self.height() - int(245 * s))))
        self.setMask(region)

    # ---- ratón ----
    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.arrastre = (ev.globalPosition().toPoint(), self.pos(), False)

    def mouseMoveEvent(self, ev):
        if not self.arrastre:
            return
        inicio, pos, movido = self.arrastre
        delta = ev.globalPosition().toPoint() - inicio
        if movido or delta.manhattanLength() > 5:
            self.arrastre = (inicio, pos, True)
            self.move(pos + delta)

    def mouseReleaseEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton or not self.arrastre:
            return
        movido = self.arrastre[2]
        self.arrastre = None
        if movido:
            json.dump([self.x(), self.y()], open(POS_FILE, "w"))
        else:
            self.activar()

    def contextMenuEvent(self, ev):
        menu = QMenu(self)
        menu.setStyleSheet("QMenu{background:#0b1626;color:#dff4ff;border:1px solid #2f7fbf;padding:4px}"
                           "QMenu::item{padding:6px 18px}QMenu::item:selected{background:#16406a}")
        menu.addAction("Hablar", self.activar)
        menu.addAction("Nueva conversación", self.nueva_conversacion)
        menu.addSeparator()
        menu.addAction("Cerrar asistente", QApplication.quit)
        menu.exec(ev.globalPos())

    def colocar(self):
        try:
            x, y = json.load(open(POS_FILE))
        except (OSError, ValueError):
            zona = QApplication.primaryScreen().availableGeometry()
            x, y = zona.right() - self.width() - 20, zona.bottom() - self.height() - 10
        self.move(x, y)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("asistente-claude")
    app.setQuitOnLastWindowClosed(False)
    try:  # ¿ya hay una esfera abierta? Entonces solo reenviamos la orden.
        nucleo.enviar("activar" if "--activar" in sys.argv else "")
        return
    except OSError:
        pass
    orbe = Orbe()
    orbe.show()
    cargar_script_kwin(orbe)
    if "--activar" in sys.argv:
        orbe.activar()
    app.exec()
    kwin_scripting().call("unloadScript", NOMBRE_SCRIPT)
    QLocalServer.removeServer(nucleo.SOCKET)


if __name__ == "__main__":
    main()
