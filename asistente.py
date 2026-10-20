#!/usr/bin/env python3
"""Asistente de voz de Claude, de pulsar para hablar.

Ejecutado directamente (atajo de teclado) le dice a la esfera (orbe.py) que
empiece a escuchar, o la arranca si no está abierta. Pulsarlo mientras está
activa cancela lo que esté haciendo.

La clase Asistente hace el trabajo: escucha tu petición (Vosk detecta cuándo
terminas de hablar y Whisper transcribe, todo sin internet),
se la pasa a Claude Code en modo solo lectura y lee la respuesta con Piper. Si la
tarea requiere hacer cambios, Claude explica qué haría y pide confirmación por
voz; solo con un "sí" se ejecuta.
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
import unicodedata

DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = {
    # Micrófono (nombre de `pactl list short sources`); vacío = el predeterminado.
    "microfono": "",
    # Modelo de faster-whisper (tiny, base, small, medium, large-v3...); vacío = solo Vosk.
    "whisper": "small",
    # Voz de Piper (archivo en voces/ sin .onnx); si no existe se usa espeak-ng.
    "voz_piper": "es_MX-claude-high",
    "velocidad_piper": 1.0,  # >1 más lento, <1 más rápido
    "voz": "es",            # voz de espeak-ng (respaldo)
    "velocidad": 165,
    # Permisos al ejecutar una acción ya confirmada: "auto" o "acceptEdits" (más restrictivo).
    "permisos_confirmado": "auto",
    "directorio": os.path.expanduser("~"),
    "segundos_max_orden": 30,
    "segundos_silencio": 1.2,  # pausa tras la que deja de escucharte
    "segundos_confirmacion": 6,
    "timeout_claude": 600,
    "escala_orbe": 1.0,
    "colores_fondo": True,  # la esfera toma los colores del fondo de escritorio
    "subtitulos": True,
}
CONFIG_PATH = os.path.join(DIR, "config.json")
if os.path.exists(CONFIG_PATH):
    CONFIG.update(json.load(open(CONFIG_PATH)))
CONFIG["directorio"] = os.path.expanduser(CONFIG["directorio"])

RATE = 16000
CHUNK = 2000  # 62,5 ms de audio s16le
SESSION_FILE = os.path.join(DIR, ".sesion")
LOG_FILE = os.path.join(DIR, "asistente.log")
SOCKET = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "asistente-claude.sock")
MARCA = "[CONFIRMAR]"
PROMPT_SISTEMA = (
    "Estás funcionando como asistente de voz en el PC Linux del usuario "
    f"(escritorio: {os.environ.get('XDG_CURRENT_DESKTOP', 'desconocido')}, "
    f"sesión: {os.environ.get('XDG_SESSION_TYPE', 'desconocida')}). "
    "La orden viene de reconocimiento de voz y puede tener errores de transcripción: interpreta la intención. "
    "Tu respuesta final se leerá en voz alta: en español, breve (1-3 frases), sin markdown, sin listas, "
    "sin bloques de código ni rutas largas. "
    f"Ahora mismo estás en modo solo lectura. Si la petición requiere cambiar algo (ejecutar programas, "
    f"editar o borrar archivos, instalar, cambiar ajustes...), NO lo intentes: explica en una frase qué harías "
    f"y termina tu respuesta exactamente con {MARCA}. El usuario confirmará por voz y entonces podrás hacerlo. "
    "La esfera puede mostrar un dato breve en lugar de su cara: si tu respuesta incluye la hora, el clima, "
    "una temperatura, un temporizador o un número corto, añade al final [MOSTRAR icono|texto grande|texto pequeño], "
    "con icono uno de: reloj, sol, luna, nubes, parcial, lluvia, tormenta, nieve, niebla, dato. "
    "Ejemplos: [MOSTRAR reloj|17:42|martes 20 de octubre], [MOSTRAR lluvia|14°|Madrid, lluvia ligera], "
    "[MOSTRAR dato|1.234 €|total]. No se lee en voz alta; no lo uses si no aporta nada."
)
TARJETA = re.compile(r"\[MOSTRAR\s+([^\]]*)\]")
SI = {"si", "hazlo", "dale", "vale", "adelante", "claro", "ok", "okay", "confirmo"}
CANCELAR = {"nada", "cancela", "cancelar", "olvidalo", "no"}


def log(*a):
    with open(LOG_FILE, "a") as f:
        print(time.strftime("[%Y-%m-%d %H:%M:%S]"), *a, file=f)


def normalizar(t):
    t = unicodedata.normalize("NFD", t.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn").strip(" .,!?¿¡")


def extraer_tarjeta(t):
    """Quita la etiqueta [MOSTRAR icono|grande|pequeño] y devuelve (texto, tarjeta o None)."""
    m = TARJETA.search(t)
    if not m:
        return t, None
    partes = [x.strip() for x in m.group(1).split("|")] + ["", ""]
    tarjeta = {"icono": partes[0].lower(), "grande": partes[1], "pequeno": partes[2]}
    return TARJETA.sub("", t).strip(), tarjeta


def limpiar_para_voz(t):
    t = TARJETA.sub("", t)
    t = re.sub(r"```.*?```", " (código omitido) ", t, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"[*_#>|]+", "", t)
    return re.sub(r"\s+", " ", t).strip()


def pitido():
    subprocess.Popen(["paplay", "/usr/share/sounds/freedesktop/stereo/message.oga"],
                     stderr=subprocess.DEVNULL)


def leer_sesion():
    return open(SESSION_FILE).read().strip() if os.path.exists(SESSION_FILE) else None


def borrar_sesion():
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)


class Cancelado(Exception):
    pass


class Asistente:
    """`avisar(estado, **datos)` informa a la interfaz de lo que está pasando.

    Estados: dormida, escuchando, pensando, hablando, confirmando, error; además
    "nivel" (volumen del micrófono, 0-1) mientras escucha.
    """

    def __init__(self, avisar=lambda estado, **datos: None):
        import numpy
        import vosk
        self.np = numpy
        self.vosk = vosk
        self.avisar = avisar
        self.hijo = None
        self.cancelado = False
        self.ocupado = False
        vosk.SetLogLevel(-1)
        self.modelo = vosk.Model(os.path.join(DIR, "modelo"))
        self.whisper = None
        if CONFIG["whisper"]:
            try:
                from faster_whisper import WhisperModel
                self.whisper = WhisperModel(CONFIG["whisper"], device="cpu", compute_type="int8")
            except Exception as e:
                log("No se pudo cargar Whisper, uso Vosk:", repr(e))
        self.voz = None
        modelo_voz = os.path.join(DIR, "voces", CONFIG["voz_piper"] + ".onnx")
        if os.path.exists(modelo_voz):
            from piper import PiperVoice
            from piper.config import SynthesisConfig
            self.voz = PiperVoice.load(modelo_voz)
            self.config_voz = SynthesisConfig(length_scale=CONFIG["velocidad_piper"])

    def cancelar(self):
        self.cancelado = True
        if self.hijo and self.hijo.poll() is None:
            self.hijo.kill()

    def _comprobar(self):
        if self.cancelado:
            raise Cancelado

    def ejecutar(self, cmd, timeout=None, input=None, **kw):
        self._comprobar()
        self.hijo = subprocess.Popen(cmd, **kw)
        try:
            return self.hijo.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.hijo.kill()
            raise
        finally:
            self.hijo = None
            self._comprobar()

    def hablar(self, texto, estado="hablando"):
        texto = limpiar_para_voz(texto)
        if not texto:
            return
        if not self.voz:
            self.avisar(estado, texto=texto, envolvente=[], fps=60)
            self.ejecutar(["espeak-ng", "-v", CONFIG["voz"], "-s", str(CONFIG["velocidad"]), texto])
            return
        audio = b"".join(c.audio_int16_bytes for c in self.voz.synthesize(texto, self.config_voz))
        self._comprobar()
        tasa = self.voz.config.sample_rate
        # Volumen a 60 fps para que la esfera "hable" al ritmo de la voz.
        muestras = self.np.frombuffer(audio, dtype=self.np.int16).astype(self.np.float32)
        paso = tasa // 60
        n = len(muestras) // paso
        rms = self.np.sqrt((muestras[:n * paso].reshape(n, paso) ** 2).mean(axis=1))
        envolvente = (rms / (rms.max() or 1)).round(3).tolist()
        self.avisar(estado, texto=texto, envolvente=envolvente, fps=60)
        self.ejecutar(["pacat", "--raw", "--format=s16le", f"--rate={tasa}", "--channels=1"],
                      stdin=subprocess.PIPE, input=audio)

    def escuchar(self, max_segundos):
        """Graba hasta que dejas de hablar (o se acaba el tiempo) y devuelve el texto.

        Vosk corta en cada pausa corta; aquí se juntan esas frases y solo se para
        tras `segundos_silencio` sin oír nada nuevo.
        """
        cmd = ["parecord", "--raw", "--format=s16le", f"--rate={RATE}", "--channels=1",
               "--latency-msec=60"]
        if CONFIG["microfono"]:
            cmd.append(f"--device={CONFIG['microfono']}")
        self.hijo = mic = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        rec = self.vosk.KaldiRecognizer(self.modelo, RATE)
        limite = time.time() + max_segundos
        partes, parcial, t_voz = [], "", 0.0
        grabado = []
        try:
            while time.time() < limite:
                self._comprobar()
                data = mic.stdout.read(CHUNK)
                if not data:
                    break
                grabado.append(data)
                x = self.np.frombuffer(data, dtype=self.np.int16).astype(self.np.float32)
                self.avisar("nivel", valor=min(1.0, float(self.np.sqrt((x ** 2).mean())) / 2500))
                if rec.AcceptWaveform(data):
                    frase = json.loads(rec.Result())["text"]
                    parcial = ""
                    if frase:
                        partes.append(frase)
                        t_voz = time.time()
                else:
                    nuevo = json.loads(rec.PartialResult())["partial"]
                    if nuevo != parcial:
                        parcial = nuevo
                        t_voz = time.time()
                if partes and not parcial and time.time() - t_voz > CONFIG["segundos_silencio"]:
                    break
            else:
                partes.append(json.loads(rec.FinalResult())["text"])
        finally:
            mic.terminate()
            self.hijo = None
        self._comprobar()
        texto = " ".join(p for p in partes if p)
        if texto and self.whisper:
            texto = self.transcribir(b"".join(grabado)) or texto
        return texto

    def transcribir(self, audio):
        """Transcribe con Whisper el audio grabado (s16le, 16 kHz)."""
        try:
            x = self.np.frombuffer(audio, dtype=self.np.int16).astype(self.np.float32) / 32768
            segmentos, _ = self.whisper.transcribe(x, language="es", beam_size=5,
                                                   vad_filter=True, condition_on_previous_text=False)
            texto = " ".join(s.text.strip() for s in segmentos).strip()
            log("Vosk/Whisper:", texto)
            return texto
        except Exception as e:
            log("Error de Whisper:", repr(e))
            return ""

    def claude(self, orden, permisos, sesion, reintentar=True):
        cmd = ["claude", "-p", orden, "--output-format", "json", "--permission-mode", permisos,
               "--append-system-prompt", PROMPT_SISTEMA]
        if sesion:
            cmd += ["--resume", sesion]
        try:
            out, err = self.ejecutar(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     cwd=CONFIG["directorio"], timeout=CONFIG["timeout_claude"])
        except subprocess.TimeoutExpired:
            return "Claude tardó demasiado en responder."
        try:
            datos = json.loads(out)
        except json.JSONDecodeError:
            log("Error de claude:", err or out)
            if sesion and reintentar:  # la sesión guardada pudo caducar; reintenta limpia
                borrar_sesion()
                return self.claude(orden, permisos, None, reintentar=False)
            return "Hubo un error al hablar con Claude."
        if datos.get("session_id"):
            open(SESSION_FILE, "w").write(datos["session_id"])
        return datos.get("result") or "Listo."

    def responder(self, texto, estado="hablando"):
        texto, tarjeta = extraer_tarjeta(texto)
        if tarjeta:
            self.avisar("mostrar", **tarjeta)
        log("Respuesta:", texto)
        self.hablar(texto, estado)

    def conversar(self):
        """Una interacción completa: escuchar, pensar, responder (y confirmar si hace falta)."""
        self.ocupado = True
        self.cancelado = False
        try:
            self._conversar()
        except Cancelado:
            log("Cancelado")
        except Exception as e:
            log("Error:", repr(e))
            self.avisar("error", texto="Algo falló. Mira asistente.log")
            time.sleep(3)
        finally:
            self.ocupado = False
            self.avisar("dormida")

    def _conversar(self):
        self.avisar("escuchando")
        pitido()
        orden = self.escuchar(CONFIG["segundos_max_orden"])
        n = normalizar(orden)
        if not n or n in CANCELAR:
            log("No se oyó ninguna orden (micrófono:", CONFIG["microfono"] or "predeterminado", ")")
            return
        if "nueva conversacion" in n or "olvida todo" in n:
            borrar_sesion()
            self.responder("Empezamos una conversación nueva.")
            return

        log("Orden:", orden)
        self.avisar("pensando", texto=orden)
        respuesta = self.claude(orden, "plan", leer_sesion())
        if MARCA not in respuesta:
            self.responder(respuesta)
            return

        self.responder(respuesta.replace(MARCA, "").strip() + " ¿Lo hago?", "confirmando")
        pitido()
        self.avisar("confirmando", texto="Di «sí» para confirmar")
        confirmacion = normalizar(self.escuchar(CONFIG["segundos_confirmacion"]))
        log("Confirmación:", confirmacion)
        palabras = set(confirmacion.split())
        if not SI & palabras or "no" in palabras:
            self.responder("Vale, no hago nada.")
            return
        self.avisar("pensando", texto=orden)
        self.responder(self.claude("El usuario ha confirmado por voz. Hazlo ahora y resume qué hiciste.",
                                   CONFIG["permisos_confirmado"], leer_sesion()))


def enviar(orden):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCKET)
    s.sendall(orden.encode())
    s.close()


if __name__ == "__main__":
    try:
        enviar(sys.argv[1] if len(sys.argv) > 1 else "activar")
    except OSError:  # la esfera no está abierta: arrancarla ya escuchando
        subprocess.Popen([sys.executable, os.path.join(DIR, "orbe.py"), "--activar"],
                         start_new_session=True, stdout=subprocess.DEVNULL,
                         stderr=open(LOG_FILE, "a"))
