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
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
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
    # Modelo para las preguntas (rápido) y para hacer lo que confirmas (más capaz); "" = el predeterminado.
    "modelo": "sonnet",
    "modelo_acciones": "opus",
    "esfuerzo": "low",  # cuánto piensa en las preguntas: low, medium, high ("" = predeterminado)
    # Tras estos minutos sin hablarle empieza una conversación nueva (el historial largo la hace lenta).
    "minutos_conversacion": 30,
    "escala_orbe": 1.0,
    "colores_fondo": True,
    "oye_claude": True,  # despertar diciendo «oye Claude» (escucha local con Vosk; nada sale del PC)  # la esfera toma los colores del fondo de escritorio
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
    "con icono uno de: reloj, sol, luna, nubes, parcial, lluvia, tormenta, nieve, niebla, dato, temporizador. "
    "Ejemplos: [MOSTRAR reloj|17:42|martes 20 de octubre], [MOSTRAR lluvia|14°|Madrid, lluvia ligera], "
    "[MOSTRAR dato|1.234 €|total], [MOSTRAR temporizador|5:00|pasta]. No se lee en voz alta; no lo uses si no aporta nada. "
    "Para un temporizador o cuenta atrás no ejecutes nada ni pidas confirmación: con esa etiqueta (duración en m:ss "
    "o h:mm:ss) la esfera lleva la cuenta, la muestra y avisa con un sonido al llegar a cero. "
    "Para recordatorios y alarmas tampoco ejecutes nada ni pidas confirmación: añade [RECORDAR AAAA-MM-DD HH:MM|texto breve] "
    "con la fecha y hora absolutas (calcúlalas a partir de la fecha actual); a esa hora la esfera sonará, lo mostrará "
    "y lo dirá en voz alta, aunque se reinicie el PC. Puedes poner varias etiquetas. Los pendientes se guardan en "
    f"{os.path.join(DIR, 'recordatorios.json')} (para borrar alguno hay que editar ese archivo). "
    "Para el clima no busques en la web: usa curl -s 'wttr.in/CIUDAD?format=%l|%C|%t|%f|%h|%w&lang=es&m' "
    "(sin ciudad usa la ubicación del usuario; para el pronóstico, 'wttr.in/CIUDAD?format=j1&lang=es')."
)
TARJETA = re.compile(r"\[MOSTRAR\s+([^\]]*)\]")
RECORDAR = re.compile(r"\[RECORDAR\s+(\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2})\s*\|([^\]]*)\]")
# «Oye Claude»: Vosk solo puede elegir entre estas frases; las palabras de relleno absorben lo demás
# para que «hola Claudia» u «oye claro» no se confundan con la frase.
FRASE_DESPERTAR = re.compile(r"\b(oye|hey) (claude|cloud|clo)\b")
GRAMATICA_DESPERTAR = json.dumps(
    ["oye claude", "oye cloud", "oye clo", "hey claude", "[unk]"]
    + ("hola claudia claro cloro oro todo como cómo que qué sí no ya hoy oigo oiga ojo ok okay voy vamos "
       "ahora oye bueno entonces la el los de y a con por para en mira hey").split())
SI = {"si", "hazlo", "dale", "vale", "adelante", "claro", "ok", "okay", "confirmo"}
CANCELAR = {"nada", "cancela", "cancelar", "olvidalo", "no"}


def log(*a):
    with open(LOG_FILE, "a") as f:
        print(time.strftime("[%Y-%m-%d %H:%M:%S]"), *a, file=f)


def normalizar(t):
    t = unicodedata.normalize("NFD", t.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn").strip(" .,!?¿¡")


def tarjeta_de(m):
    partes = [x.strip() for x in m.group(1).split("|")] + ["", ""]
    return {"icono": partes[0].lower(), "grande": partes[1], "pequeno": partes[2]}


def recordatorio_de(m):
    return {"cuando": m.group(1).replace("T", " "), "texto": m.group(2).strip()}


def extraer_tarjeta(t):
    """Quita la etiqueta [MOSTRAR icono|grande|pequeño] y devuelve (texto, tarjeta o None)."""
    m = TARJETA.search(t)
    if not m:
        return t, None
    return TARJETA.sub("", t).strip(), tarjeta_de(m)


def extraer_recordatorios(t):
    """Quita las etiquetas [RECORDAR fecha hora|texto] y devuelve (texto, [recordatorios])."""
    lista = [recordatorio_de(m) for m in RECORDAR.finditer(t)]
    return RECORDAR.sub("", t).strip(), lista


def limpiar_para_voz(t):
    t = RECORDAR.sub("", TARJETA.sub("", t)).replace(MARCA, "")
    t = re.sub(r"```.*?```", " (código omitido) ", t, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"[*_#>|]+", "", t)
    return re.sub(r"\s+", " ", t).strip()


def pitido():
    subprocess.Popen(["paplay", "/usr/share/sounds/freedesktop/stereo/message.oga"],
                     stderr=subprocess.DEVNULL)


def leer_sesion():
    if not os.path.exists(SESSION_FILE):
        return None
    if time.time() - os.path.getmtime(SESSION_FILE) > CONFIG["minutos_conversacion"] * 60:
        log("Conversación nueva tras", CONFIG["minutos_conversacion"], "minutos sin hablar")
        borrar_sesion()
        return None
    return open(SESSION_FILE).read().strip()


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
        self.hijo_claude = None
        self.cancelado = False
        self.ocupado = False
        self.pausa_despertador = False  # p. ej. mientras hay un juego a pantalla completa
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
        for hijo in (self.hijo, self.hijo_claude):
            if hijo and hijo.poll() is None:
                hijo.kill()

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
            segmentos, _ = self.whisper.transcribe(x, language="es", beam_size=5, hotwords="Claude",
                                                   vad_filter=True, condition_on_previous_text=False)
            texto = " ".join(s.text.strip() for s in segmentos).strip()
            log("Vosk/Whisper:", texto)
            return texto
        except Exception as e:
            log("Error de Whisper:", repr(e))
            return ""

    def claude(self, orden, permisos, sesion, modelo="", esfuerzo="", reintentar=True):
        """Llama a Claude Code y va diciendo la respuesta en voz alta frase a frase según llega.

        Las etiquetas ([MOSTRAR], [RECORDAR]) se aplican en cuanto aparecen. Devuelve
        (texto completo, si ya se dijo o mostró algo).
        """
        prompt = PROMPT_SISTEMA + time.strftime(" Fecha y hora actuales: %Y-%m-%d %H:%M (%A).")
        cmd = ["claude", "-p", orden, "--output-format", "stream-json", "--verbose", "--include-partial-messages",
               "--permission-mode", permisos, "--append-system-prompt", prompt]
        if modelo:
            cmd += ["--model", modelo]
        if esfuerzo:
            cmd += ["--effort", esfuerzo]
        if sesion:
            cmd += ["--resume", sesion]
        self._comprobar()
        errores = tempfile.TemporaryFile("w+")
        proc = self.hijo_claude = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errores, text=True,
                                                   cwd=CONFIG["directorio"])
        caducado = threading.Event()
        reloj = threading.Timer(CONFIG["timeout_claude"], lambda: (caducado.set(), proc.kill()))
        reloj.start()
        cola = queue.Queue()
        locutor = threading.Thread(target=self._locutor, args=(cola,), daemon=True)
        locutor.start()
        buffer, resultado, hablado = "", None, False
        try:
            for linea in proc.stdout:
                try:
                    ev = json.loads(linea)
                except ValueError:
                    continue
                if ev.get("type") == "result":
                    resultado = ev
                if ev.get("type") != "stream_event":
                    continue
                e = ev["event"]
                if e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta":
                    buffer += e["delta"]["text"]
                elif e.get("type") != "content_block_stop":
                    continue
                frases, buffer, etiquetas = self._trocear(buffer, final=e.get("type") == "content_block_stop")
                for f in frases:
                    cola.put(f)
                hablado = hablado or bool(frases) or etiquetas
        finally:
            reloj.cancel()
            cola.put(None)
            locutor.join()
            proc.wait()
            self.hijo_claude = None
        self._comprobar()
        if not resultado or resultado.get("is_error"):
            errores.seek(0)
            log("Error de claude:", errores.read()[-2000:] or resultado)
            if caducado.is_set():
                return "Claude tardó demasiado en responder.", hablado
            if sesion and reintentar and not hablado:  # la sesión guardada pudo caducar; reintenta limpia
                borrar_sesion()
                return self.claude(orden, permisos, None, modelo, esfuerzo, reintentar=False)
            return "Hubo un error al hablar con Claude.", hablado
        if resultado.get("session_id"):
            open(SESSION_FILE, "w").write(resultado["session_id"])
        return resultado.get("result") or "Listo.", hablado

    def _trocear(self, buffer, final=False):
        """Separa del texto recibido las frases ya completas (para decirlas) y aplica las etiquetas.

        Devuelve (frases, resto del buffer, si había etiquetas).
        """
        etiquetas = False
        for m in TARJETA.finditer(buffer):
            self.avisar("mostrar", **tarjeta_de(m))
            etiquetas = True
        for m in RECORDAR.finditer(buffer):
            self.avisar("recordar", **recordatorio_de(m))
            etiquetas = True
        buffer = RECORDAR.sub("", TARJETA.sub("", buffer))
        if final:
            corte = len(buffer)
        else:
            # No cortar dentro de una etiqueta a medio llegar, ni en trozos demasiado cortos.
            limite = buffer.rfind("[") if buffer.rfind("[") > buffer.rfind("]") else len(buffer)
            fines = [m.end() for m in re.finditer(r"[.!?…:;][\"»)]?(?=\s)|\n", buffer[:limite]) if m.end() >= 25]
            corte = fines[-1] if fines else 0
        frase = limpiar_para_voz(buffer[:corte])
        return ([frase] if frase else []), buffer[corte:], etiquetas

    def _locutor(self, cola):
        """Hilo que dice en orden las frases que van llegando."""
        while (frase := cola.get()) is not None:
            if self.cancelado:
                continue
            try:
                self.hablar(frase)
            except Cancelado:
                pass

    def responder(self, texto, estado="hablando"):
        texto, tarjeta = extraer_tarjeta(texto)
        texto, recordatorios = extraer_recordatorios(texto)
        for r in recordatorios:
            self.avisar("recordar", **r)
        if tarjeta:
            self.avisar("mostrar", **tarjeta)
        log("Respuesta:", texto)
        self.hablar(texto, estado)

    def anunciar(self, texto):
        """Dice algo por iniciativa propia (p. ej. un recordatorio)."""
        self.ocupado = True
        self.cancelado = False
        try:
            self.responder(texto)
        except Cancelado:
            log("Cancelado")
        finally:
            self.ocupado = False
            self.avisar("dormida")

    def vigilar(self, al_oir):
        """Hilo de fondo: escucha solo para detectar «oye Claude» y llama a al_oir(orden).

        Si la frase sigue («oye Claude, ¿qué hora es?») se transcribe con Whisper y se pasa
        el resto como orden; si no, orden = "" y se escucha después del pitido.
        """
        cmd = ["parecord", "--raw", "--format=s16le", f"--rate={RATE}", "--channels=1", "--latency-msec=60"]
        if CONFIG["microfono"]:
            cmd.append(f"--device={CONFIG['microfono']}")
        mic = rec = None
        trozos = []
        while True:
            if self.ocupado or self.pausa_despertador or not CONFIG["oye_claude"]:
                if mic:
                    mic.terminate()
                    mic = None
                time.sleep(0.3)
                continue
            if mic is None:
                mic = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                rec = self.vosk.KaldiRecognizer(self.modelo, RATE, GRAMATICA_DESPERTAR)
                rec.SetWords(True)
                trozos = []
            data = mic.stdout.read(CHUNK)
            if not data:
                mic = None
                time.sleep(2)
                continue
            trozos = trozos[-160:] + [data]  # hasta ~10 s de la frase en curso
            if not rec.AcceptWaveform(data):
                continue
            resultado = json.loads(rec.Result())
            texto, audio, trozos = resultado.get("text", ""), b"".join(trozos), []
            m = FRASE_DESPERTAR.search(texto)
            if not m:
                continue
            confianza = [w["conf"] for w in resultado.get("result", []) if w["word"] in m.group(0).split()]
            if min(confianza or [0]) < 0.8:
                continue
            mic.terminate()
            mic = None
            log("Despertado:", texto)
            orden = ""
            if texto[m.end():].strip() and self.whisper:
                orden = re.sub(r"^\W*(oye|hey)\W+\w+\W*", "", self.transcribir(audio), flags=re.I).strip()
            al_oir(orden)
            time.sleep(1.5)  # deja que empiece la conversación antes de volver a vigilar

    def conversar(self, orden=None):
        """Una interacción completa: escuchar, pensar, responder (y confirmar si hace falta).

        Con `orden` (dicha ya junto a «oye Claude») se salta la escucha.
        """
        self.ocupado = True
        self.cancelado = False
        try:
            self._conversar(orden)
        except Cancelado:
            log("Cancelado")
        except Exception as e:
            log("Error:", repr(e))
            self.avisar("error", texto="Algo falló. Mira asistente.log")
            time.sleep(3)
        finally:
            self.ocupado = False
            self.avisar("dormida")

    def _conversar(self, orden=None):
        if not orden:
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
        respuesta, hablado = self.claude(orden, "plan", leer_sesion(), CONFIG["modelo"], CONFIG["esfuerzo"])
        if hablado:
            log("Respuesta:", respuesta)
        else:
            self.responder(respuesta.replace(MARCA, ""))
        if MARCA not in respuesta:
            return

        self.hablar("¿Lo hago?", "confirmando")
        pitido()
        self.avisar("confirmando", texto="Di «sí» para confirmar")
        confirmacion = normalizar(self.escuchar(CONFIG["segundos_confirmacion"]))
        log("Confirmación:", confirmacion)
        palabras = set(confirmacion.split())
        if not SI & palabras or "no" in palabras:
            self.responder("Vale, no hago nada.")
            return
        self.avisar("pensando", texto=orden)
        respuesta, hablado = self.claude("El usuario ha confirmado por voz. Hazlo ahora y resume qué hiciste.",
                                         CONFIG["permisos_confirmado"], leer_sesion(), CONFIG["modelo_acciones"])
        if hablado:
            log("Respuesta:", respuesta)
        else:
            self.responder(respuesta)


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
