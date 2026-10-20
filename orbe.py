#!/usr/bin/env python3
"""Esfera flotante del asistente: una gota de energía con ojos que duerme en el escritorio y se
ilumina al hablar con él.

Clic: hablar (o cancelar si está ocupado). Arrastrar: moverla. Clic derecho: menú.
El atajo de teclado (asistente.py) le manda órdenes por un socket local.
"""
import colorsys
import glob
import json
import math
import os
import random
import re
import sys
import threading
import time

# XWayland: en Wayland puro una ventana no puede elegir su posición ni quedarse encima.
os.environ["QT_QPA_PLATFORM"] = "xcb"

from PyQt6.QtCore import QObject, QPoint, QPointF, QRect, QRectF, Qt, QTimer, pyqtClassInfo, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusConnection, QDBusInterface
from PyQt6.QtGui import QColor, QCursor, QFont, QFontMetrics, QImage, QPainter, QPainterPath, QPen, QRadialGradient, QRegion
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
# Ojos por estado: apertura (0 cerrados, 1 normales) y alegría (1 = ojos en arco, sonriendo).
OJOS = {
    "cargando":    (0.35, 0.0),
    "dormida":     (0.0, 0.0),
    "escuchando":  (1.15, 0.0),
    "pensando":    (0.7, 0.0),
    "hablando":    (1.0, 0.0),
    "confirmando": (1.05, 0.2),
    "error":       (0.55, 0.0),
}
ANCHO, ALTO = 420, 360
RADIO = 46
RETRASO_AUDIO = 0.12
SEGUNDOS_TARJETA = 10  # lo que sigue visible la tarjeta tras terminar de hablar  # lo que tarda el audio en salir por los altavoces


def mezclar(a, b, k):
    return a + (b - a) * k


def imagen_fondo():
    """Imagen del fondo de escritorio actual (vista previa si es de Wallpaper Engine)."""
    try:
        texto = open(os.path.expanduser("~/.config/plasma-org.kde.plasma.desktop-appletsrc")).read()
    except OSError:
        return None
    for linea in texto.splitlines():
        clave, _, valor = linea.partition("=")
        valor = valor.replace("file://", "")
        if clave.startswith("WallpaperSource"):
            carpeta = os.path.dirname(valor.split("+")[0])
            previas = glob.glob(os.path.join(carpeta, "preview.*"))
            if previas:
                return previas[0]
        elif clave == "Image" and os.path.isfile(valor):
            return valor
    return None


def colores_del_fondo():
    """Ajusta los colores de cada estado a los tonos dominantes del fondo de escritorio."""
    import numpy as np
    ruta = imagen_fondo()
    img = QImage(ruta) if ruta else QImage()
    if img.isNull():
        return
    img = img.convertToFormat(QImage.Format.Format_RGB888).scaled(
        80, 80, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    bits = img.constBits()
    bits.setsize(img.sizeInBytes())
    px = np.frombuffer(bits, np.uint8).reshape(img.height(), img.bytesPerLine())[:, :img.width() * 3]
    hsv = np.array([colorsys.rgb_to_hsv(*c) for c in px.reshape(-1, 3) / 255.0])
    # Tonos agrupados en 24 sectores, pesados por saturación y brillo (los grises no cuentan).
    peso = hsv[:, 1] * hsv[:, 2] * (hsv[:, 1] > 0.3) * (hsv[:, 2] > 0.3)
    sector = (hsv[:, 0] * 24).astype(int) % 24
    total = np.bincount(sector, weights=peso, minlength=24)
    if total.sum() == 0:
        return
    elegidos = []
    for b in np.argsort(-(total + 0.5 * (np.roll(total, 1) + np.roll(total, -1)))):
        if all(min(abs(b - o), 24 - abs(b - o)) >= 4 for o in elegidos):
            elegidos.append(b)
        if len(elegidos) == 2:
            break

    def tono(b):
        m = (sector == b) & (peso > 0)
        return float(np.average(hsv[m, 0], weights=peso[m]))

    principal = tono(elegidos[0])
    # Si no hay un segundo color con presencia, se usa uno que contraste (p. ej. dorado → turquesa).
    if len(elegidos) > 1 and total[elegidos[1]] > 0.08 * total[elegidos[0]]:
        acento = tono(elegidos[1])
    else:
        acento = (principal + 0.4) % 1

    def rgb(h, s, v):
        return tuple(int(x * 255) for x in colorsys.hsv_to_rgb(h % 1, s, v))

    for estado, (h, s, v) in {
        "cargando": (principal, 0.65, 0.6),
        "dormida": (principal, 0.7, 0.5),
        "escuchando": (acento, 0.6, 1.0),
        "pensando": (acento + 0.04, 0.45, 0.95),
        "hablando": (principal, 0.55, 1.0),
        "confirmando": (principal - 0.05, 0.8, 1.0),
    }.items():
        ESTADOS[estado] = (rgb(h, s, v),) + ESTADOS[estado][1:]


# ---- iconos del tiempo para las tarjetas: (painter, centro, tamaño, t) ----
SOL, NUBE, AGUA = QColor(255, 205, 80), QColor(240, 245, 252), QColor(130, 200, 255)


def _sol(p, c, s, t, escala=1.0):
    s *= escala
    pen = QPen(SOL, max(1.5, s * 0.12))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    for i in range(8):
        a = t * 0.6 + i * math.pi / 4
        p.drawLine(QPointF(c.x() + math.cos(a) * s * 0.72, c.y() + math.sin(a) * s * 0.72),
                   QPointF(c.x() + math.cos(a) * s, c.y() + math.sin(a) * s))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(SOL)
    p.drawEllipse(c, s * 0.5, s * 0.5)


def _nube(p, c, s, t, color=NUBE):
    c = QPointF(c.x() + math.sin(t * 0.8) * s * 0.06, c.y())
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    for dx, dy, rr in ((-0.45, 0.12, 0.38), (0.0, -0.12, 0.5), (0.45, 0.1, 0.4)):
        p.drawEllipse(QPointF(c.x() + dx * s, c.y() + dy * s), rr * s, rr * s)
    p.drawRoundedRect(QRectF(c.x() - 0.8 * s, c.y() + 0.05 * s, 1.6 * s, 0.45 * s), 0.2 * s, 0.2 * s)


def _caer(p, c, s, t, dibujar, vel):
    for i in range(4):
        x = c.x() + (-0.5 + i / 3) * s
        y = c.y() + s * 0.65 + ((t * vel + i * 0.37) % 1) * s * 0.6
        dibujar(QPointF(x, y))


def icono_sol(p, c, s, t):
    _sol(p, c, s, t)


def icono_luna(p, c, s, t):
    camino = QPainterPath()
    camino.addEllipse(c, s * 0.6, s * 0.6)
    hueco = QPainterPath()
    hueco.addEllipse(QPointF(c.x() + s * 0.3, c.y() - s * 0.2), s * 0.5, s * 0.5)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 236, 170))
    p.drawPath(camino.subtracted(hueco))
    for i, (dx, dy) in enumerate(((0.6, 0.45), (0.85, -0.55), (-0.8, -0.6))):
        a = 0.4 + 0.6 * abs(math.sin(t * 1.5 + i * 2))
        p.setBrush(QColor(255, 255, 255, int(255 * a)))
        p.drawEllipse(QPointF(c.x() + dx * s, c.y() + dy * s), s * 0.06, s * 0.06)


def icono_nubes(p, c, s, t):
    _nube(p, QPointF(c.x() + s * 0.25, c.y() - s * 0.2), s * 0.7, t + 1, QColor(200, 210, 225))
    _nube(p, c, s, t)


def icono_parcial(p, c, s, t):
    _sol(p, QPointF(c.x() + s * 0.3, c.y() - s * 0.3), s, t, 0.7)
    _nube(p, QPointF(c.x() - s * 0.1, c.y() + s * 0.1), s * 0.85, t)


def icono_lluvia(p, c, s, t):
    pen = QPen(AGUA, max(1.5, s * 0.1))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)

    def gota(q):
        p.setPen(pen)
        p.drawLine(q, QPointF(q.x() - s * 0.06, q.y() + s * 0.2))
    _caer(p, c, s, t, gota, 1.6)
    p.setPen(Qt.PenStyle.NoPen)
    _nube(p, QPointF(c.x(), c.y() - s * 0.15), s, t, QColor(215, 225, 240))


def icono_tormenta(p, c, s, t):
    icono_lluvia(p, c, s, t)
    if (t % 2.2) < 0.25:
        rayo = QPainterPath(QPointF(c.x() + s * 0.05, c.y() + s * 0.3))
        for dx, dy in ((-0.2, 0.75), (0.02, 0.72), (-0.12, 1.15), (0.25, 0.6), (0.03, 0.62), (0.15, 0.3)):
            rayo.lineTo(QPointF(c.x() + dx * s, c.y() + dy * s))
        p.setBrush(QColor(255, 225, 90))
        p.drawPath(rayo)


def icono_nieve(p, c, s, t):
    def copo(q):
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(q, s * 0.08, s * 0.08)
    p.setPen(Qt.PenStyle.NoPen)
    _caer(p, c, s, t, copo, 0.6)
    _nube(p, QPointF(c.x(), c.y() - s * 0.15), s, t)


def icono_niebla(p, c, s, t):
    pen = QPen(QColor(235, 240, 248, 220), max(2.0, s * 0.14))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    for i in range(4):
        dx = math.sin(t * 0.9 + i * 1.7) * s * 0.18
        y = c.y() + (-0.45 + i * 0.3) * s
        p.drawLine(QPointF(c.x() - s * 0.75 + dx, y), QPointF(c.x() + s * 0.75 + dx, y))
    p.setPen(Qt.PenStyle.NoPen)


ICONOS = {"sol": icono_sol, "luna": icono_luna, "nubes": icono_nubes, "parcial": icono_parcial,
          "lluvia": icono_lluvia, "tormenta": icono_tormenta, "nieve": icono_nieve, "niebla": icono_niebla}


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

        if nucleo.CONFIG.get("colores_fondo", True):
            try:
                colores_del_fondo()
            except Exception as e:
                nucleo.log("No se pudieron sacar los colores del fondo:", repr(e))
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
        self.apertura, self.alegria = 0.3, 0.0
        self.mirada = [0.0, 0.0]
        self.sorpresa, self.inclinacion, self.t_feliz = 0.0, 0.0, 0.0
        self.tarjeta, self.mezcla_tarjeta, self.t_fin_tarjeta = None, 0.0, 0.0
        self.objetivo_mirada, self.t_mirada = (0.0, 0.0), 0.0
        self.cursor_previo, self.t_cursor = QCursor.pos(), 0.0
        self.t_parpadeo = time.time() + random.uniform(2, 5)
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
        self.sorpresa = 1.0
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
        if estado == "mostrar":
            self.mostrar_tarjeta(datos)
            return
        if estado == "nivel":
            self.nivel_mic = datos["valor"]
            return
        if self.estado == "hablando" and estado == "dormida":
            self.t_feliz = time.time() + 1.8
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
        self.animar_ojos(ahora, t)
        # La tarjeta (hora, clima...) sustituye a la cara hasta unos segundos después de terminar.
        if self.tarjeta and self.estado == "escuchando":
            self.tarjeta = None
        if self.tarjeta and self.estado != "dormida":
            self.t_fin_tarjeta = ahora + SEGUNDOS_TARJETA
        activa = bool(self.tarjeta) and ahora < self.t_fin_tarjeta
        self.mezcla_tarjeta = mezclar(self.mezcla_tarjeta, 1.0 if activa else 0.0, 0.1)
        if not activa and self.mezcla_tarjeta < 0.01:
            self.mezcla_tarjeta, self.tarjeta = 0.0, None

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

    def animar_ojos(self, ahora, t):
        apertura, alegria = OJOS[self.estado]
        if self.estado == "escuchando":
            apertura += 0.15 * self.nivel_mic
        if ahora < self.t_feliz:  # sonrisa al terminar de hablar
            alegria = 1.0
        self.sorpresa *= 0.94
        apertura += 0.5 * self.sorpresa
        # Parpadeo: unos 150 ms cada pocos segundos (a veces doble).
        if ahora >= self.t_parpadeo:
            fase = (ahora - self.t_parpadeo) / 0.15
            if fase < 1:
                apertura *= abs(1 - 2 * fase)
            else:
                self.t_parpadeo = ahora + (0.25 if random.random() < 0.15 else random.uniform(2.5, 6))
        self.apertura = mezclar(self.apertura, apertura, 0.5 if apertura < self.apertura else 0.2)
        self.alegria = mezclar(self.alegria, alegria, 0.12)

        # Mirada: sigue al ratón si se mueve; si no, echa vistazos al azar. Pensando, mira hacia arriba.
        cursor = QCursor.pos()
        if (cursor - self.cursor_previo).manhattanLength() > 3:
            self.cursor_previo, self.t_cursor = cursor, ahora
        if self.estado == "pensando":
            objetivo = (0.6 * math.sin(t * 1.3), -0.85)
        elif self.estado == "dormida" and ahora >= self.t_feliz:
            objetivo = (0.0, 0.35)
        elif ahora - self.t_cursor < 3:
            s = self.escala
            dx = cursor.x() - (self.x() + ANCHO / 2 * s)
            dy = cursor.y() - (self.y() + 130 * s)
            d = math.hypot(dx, dy) or 1
            k = min(1.0, d / 250)
            objetivo = (dx / d * k, dy / d * k)
        else:
            if ahora >= self.t_mirada:
                self.objetivo_mirada = random.choice([(0.0, 0.0)] * 3 + [
                    (random.uniform(-0.9, 0.9), random.uniform(-0.6, 0.6))])
                self.t_mirada = ahora + random.uniform(1.5, 4.5)
            objetivo = self.objetivo_mirada
        self.mirada = [mezclar(m, o, 0.12) for m, o in zip(self.mirada, objetivo)]

        # Cabeza: se inclina al pensar (y un poco hacia donde mira).
        inclinacion = 0.05 * self.mirada[0]
        if self.estado == "pensando":
            inclinacion += 0.14 + 0.06 * math.sin(t * 0.9)
        self.inclinacion = mezclar(self.inclinacion, inclinacion, 0.06)

    def dibujar_cara(self, p, c, r, tono):
        """Ojos de luz, sin pupilas: óvalos inclinados que parpadean y miran."""
        mx, my = self.mirada
        hw = r * 0.14
        hh = r * 0.23 * self.apertura
        ojo_y = c.y() - r * 0.06 + my * r * 0.12
        luz = QColor(*[int(x) for x in self.color]).lighter(190)
        intenso = QColor(*[int(x) for x in self.color]).darker(175)

        def con_alfa(q, a):
            q = QColor(q)
            q.setAlphaF(max(0.0, min(1.0, a)))
            return q

        for lado in (-1, 1):
            ex = c.x() + lado * r * 0.33 + mx * r * 0.18
            # Contorno suave del color de la esfera, más intenso, para que el ojo resalte
            g = QRadialGradient(QPointF(ex, ojo_y), r * 0.3)
            g.setColorAt(0, con_alfa(intenso, 0.75))
            g.setColorAt(0.55, con_alfa(intenso, 0.45))
            g.setColorAt(1, con_alfa(intenso, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(g)
            p.drawEllipse(QPointF(ex, ojo_y), r * 0.3, r * 0.3 * max(0.75, min(1.2, self.apertura + 0.2)))

            p.save()
            p.translate(ex, ojo_y)
            p.rotate(-lado * 10)
            normal = 1 - self.alegria
            if normal > 0.01:
                if hh < 2.2:  # cerrado: una raya de luz
                    pen = QPen(con_alfa(luz, 0.8 * normal), max(2.0, r * 0.045))
                    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                    p.setPen(pen)
                    p.drawLine(QPointF(-hw, 0), QPointF(hw, 0))
                    p.setPen(Qt.PenStyle.NoPen)
                else:
                    g = QRadialGradient(QPointF(0, -hh * 0.2), max(hw, hh))
                    g.setColorAt(0, con_alfa(QColor(255, 255, 255), normal))
                    g.setColorAt(0.6, con_alfa(luz, normal))
                    g.setColorAt(1, con_alfa(luz.darker(115), 0.9 * normal))
                    p.setBrush(g)
                    p.setPen(QPen(con_alfa(intenso, 0.8 * normal), 1.4))
                    p.drawEllipse(QPointF(0, 0), hw, hh)
                    p.setPen(Qt.PenStyle.NoPen)
            if self.alegria > 0.01:  # feliz: arcos ^ ^
                arco = QPainterPath(QPointF(-hw, hw * 0.35))
                arco.quadTo(QPointF(0, -hw * 1.1), QPointF(hw, hw * 0.35))
                pen = QPen(con_alfa(luz.lighter(115), self.alegria), max(2.5, r * 0.06))
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(arco)
                p.setPen(Qt.PenStyle.NoPen)
            p.restore()

    def mostrar_tarjeta(self, datos):
        datos = dict(datos)
        if datos.get("icono") == "reloj":
            # Se guarda la diferencia con la hora local para que el reloj siga andando (sirve para otras zonas).
            m = re.search(r"(\d{1,2})[:.](\d{2})", datos.get("grande", ""))
            ahora = time.localtime()
            local = ahora.tm_hour * 60 + ahora.tm_min
            datos["desfase"] = ((int(m.group(1)) * 60 + int(m.group(2)) - local + 720) % 1440 - 720) if m else 0
        self.tarjeta = datos
        self.t_fin_tarjeta = time.time() + SEGUNDOS_TARJETA

    def dibujar_tarjeta(self, p, c, r, t):
        """Dibuja dentro de la gota un icono animado y el dato (hora, temperatura...)."""
        d = self.tarjeta
        icono, grande, pequeno = d.get("icono", "dato"), d.get("grande", ""), d.get("pequeno", "")
        blanco = QColor(250, 252, 255)
        p.setPen(Qt.PenStyle.NoPen)
        if icono == "reloj":
            s = time.time() + d.get("desfase", 0) * 60
            hora = time.localtime(s)
            grande = time.strftime("%H:%M", hora)
            self.icono_reloj(p, QPointF(c.x(), c.y() - r * 0.18), r * 0.5, hora, s % 60)
            centro_texto, tam = c.y() + r * 0.58, r * 0.27
        elif icono == "dato" or icono not in ICONOS:
            centro_texto, tam = c.y(), r * 0.5
        else:
            ICONOS[icono](p, QPointF(c.x(), c.y() - r * 0.3), r * 0.36, t)
            centro_texto, tam = c.y() + r * 0.4, r * 0.42
        if grande:
            fuente = QFont("Noto Sans", 10, QFont.Weight.Bold)
            fuente.setPixelSize(max(8, int(tam)))
            while QFontMetrics(fuente).horizontalAdvance(grande) > r * 1.6 and fuente.pixelSize() > 8:
                fuente.setPixelSize(fuente.pixelSize() - 1)
            p.setFont(fuente)
            caja = QRectF(c.x() - r, centro_texto - tam, r * 2, tam * 2)
            p.setPen(QColor(0, 0, 0, 90))
            p.drawText(caja.translated(0, 1.5), Qt.AlignmentFlag.AlignCenter, grande)
            p.setPen(blanco)
            p.drawText(caja, Qt.AlignmentFlag.AlignCenter, grande)
        if pequeno:
            fuente = QFont("Noto Sans", 9)
            p.setFont(fuente)
            p.setPen(QColor(235, 245, 255, 230))
            p.drawText(QRectF(c.x() - 150, c.y() + r * 1.12, 300, 22), Qt.AlignmentFlag.AlignCenter,
                       QFontMetrics(fuente).elidedText(pequeno, Qt.TextElideMode.ElideRight, 290))
        p.setPen(Qt.PenStyle.NoPen)

    def icono_reloj(self, p, c, radio, hora, segundos):
        p.setBrush(QColor(255, 255, 255, 40))
        p.setPen(QPen(QColor(255, 255, 255, 220), 1.6))
        p.drawEllipse(c, radio, radio)
        for i in range(12):
            a = i * math.pi / 6
            largo = 0.8 if i % 3 == 0 else 0.87
            p.drawLine(QPointF(c.x() + math.sin(a) * radio * largo, c.y() - math.cos(a) * radio * largo),
                       QPointF(c.x() + math.sin(a) * radio * 0.95, c.y() - math.cos(a) * radio * 0.95))
        minutos = hora.tm_min + segundos / 60
        horas = hora.tm_hour % 12 + minutos / 60
        for angulo, largo, grosor, color in ((horas / 12, 0.5, 3.0, QColor(255, 255, 255)),
                                             (minutos / 60, 0.75, 2.0, QColor(255, 255, 255)),
                                             (segundos / 60, 0.82, 1.0, QColor(255, 190, 90))):
            a = angulo * 2 * math.pi
            pen = QPen(color, grosor)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawLine(c, QPointF(c.x() + math.sin(a) * radio * largo, c.y() - math.cos(a) * radio * largo))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(c, 2.2, 2.2)

    def centro(self):
        t = time.time() - self.t0
        flotar = math.sin(t * 1.1) * 5 + math.sin(t * 0.47) * 2
        return QPointF(ANCHO / 2, 130 + flotar)

    def contorno(self, c, r, t):
        """Borde de gota que ondula: más cuanto más fuerte suena la voz."""
        amp = 0.025 + 0.11 * self.energia
        camino = QPainterPath()
        n = 96
        for k in range(n + 1):
            a = 2 * math.pi * k / n
            d = (math.sin(3 * a + t * 1.7) * 0.5 + math.sin(5 * a - t * 2.3) * 0.3
                 + math.sin(2 * a + t * 0.9 + 1.3) * 0.4)
            rr = r * (1 + amp * d)
            q = QPointF(c.x() + math.cos(a) * rr, c.y() + math.sin(a) * rr)
            camino.moveTo(q) if k == 0 else camino.lineTo(q)
        camino.closeSubpath()
        return camino

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(self.escala, self.escala)
        t = time.time() - self.t0
        c = self.centro()
        e, b = self.energia, self.brillo
        m = self.mezcla_tarjeta
        r = RADIO * (1 + 0.12 * e) * (1 + 0.3 * m)
        col = QColor(*[int(x) for x in self.color])
        # Al hablar rebota y se aplasta con la voz; con la sorpresa se estira.
        voz = e if self.estado in ("hablando", "confirmando") else 0.0
        c = QPointF(c.x(), c.y() - 7 * voz)
        p.save()
        p.translate(c)
        p.rotate(math.degrees(self.inclinacion))
        p.scale(1 + 0.06 * voz - 0.05 * self.sorpresa, 1 - 0.07 * voz + 0.08 * self.sorpresa)
        p.translate(-c)

        def tono(alfa, claro=100):
            q = col.lighter(claro) if claro != 100 else QColor(col)
            q.setAlphaF(max(0.0, min(1.0, alfa)))
            return q

        p.setPen(Qt.PenStyle.NoPen)
        # Halo exterior que late
        rh = r * (2.3 + 0.4 * e)
        g = QRadialGradient(c, rh)
        g.setColorAt(0, tono(0.5 * b * (0.7 + 0.6 * e)))
        g.setColorAt(0.4, tono(0.18 * b))
        g.setColorAt(1, tono(0))
        p.setBrush(g)
        p.drawEllipse(c, rh, rh)

        # Cuerpo gelatinoso
        gota = self.contorno(c, r, t)
        g = QRadialGradient(c, r * 1.1, QPointF(c.x() - r * 0.3, c.y() - r * 0.4))
        g.setColorAt(0, tono(0.95 * b, 165))
        g.setColorAt(0.55, tono(0.75 * b, 105))
        g.setColorAt(1, tono(0.9 * b, 55))
        p.setBrush(g)
        p.drawPath(gota)

        # Plasma: manchas de luz que giran dentro
        p.save()
        p.setClipPath(gota)
        for k, (vel, dist, tam, claro) in enumerate(((0.7, 0.45, 0.65, 180), (-0.5, 0.5, 0.55, 140),
                                                     (1.1, 0.35, 0.45, 200))):
            a = t * vel * (1 + self.giro * 0.4) + k * 2.1
            q = QPointF(c.x() + math.cos(a) * r * dist, c.y() + math.sin(a * 1.3) * r * dist * 0.8)
            g = QRadialGradient(q, r * tam)
            g.setColorAt(0, tono(0.45 * b + 0.25 * e, claro))
            g.setColorAt(1, tono(0, claro))
            p.setBrush(g)
            p.drawEllipse(q, r * tam, r * tam)
        # Brillo de cristal arriba a la izquierda
        g = QRadialGradient(QPointF(c.x() - r * 0.35, c.y() - r * 0.5), r * 0.45)
        g.setColorAt(0, QColor(255, 255, 255, int(110 * b)))
        g.setColorAt(1, QColor(255, 255, 255, 0))
        p.setBrush(g)
        p.drawEllipse(QPointF(c.x() - r * 0.35, c.y() - r * 0.5), r * 0.5, r * 0.32)
        if m > 0.01:  # con tarjeta, el interior se oscurece como una pantalla para que se lea bien
            pantalla = col.darker(420)
            pantalla.setAlphaF(0.72 * m)
            p.setBrush(pantalla)
            p.drawPath(gota)
        p.restore()

        # Borde luminoso
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(tono(0.7 * b, 175), 1.6))
        p.drawPath(gota)
        p.setPen(Qt.PenStyle.NoPen)

        if m < 0.99:
            p.setOpacity(1 - m)
            self.dibujar_cara(p, c, r, tono)
        if m > 0.01 and self.tarjeta:
            p.setOpacity(m)
            self.dibujar_tarjeta(p, c, r, t)
        p.setOpacity(1)

        # Chispas en órbita (más y más lejos cuando está activa)
        extra = 14 * self.nivel_mic if self.estado == "escuchando" else 8 * e
        visibles = 0.25 + 0.75 * self.anillos
        for angulo, radio, vel in self.particulas:
            a = angulo + self.rotacion * vel
            rr = r * (radio - 0.1) + extra
            pos = QPointF(c.x() + math.cos(a) * rr, c.y() + math.sin(a) * rr * 0.85)
            tam = 1.4 + 1.2 * (0.5 + 0.5 * math.sin(t * 3 + angulo * 5))
            g = QRadialGradient(pos, tam * 3)
            g.setColorAt(0, tono(visibles * b, 190))
            g.setColorAt(1, tono(0, 190))
            p.setBrush(g)
            p.drawEllipse(pos, tam * 3, tam * 3)

        p.restore()
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
