import speech_recognition as sr
import datetime
import time
import sys
import subprocess
import os
import requests
import signal
import webbrowser
from threading import Thread, Event, Timer
from concurrent.futures import ThreadPoolExecutor
import logging
import re
from urllib.parse import quote_plus
import mysql.connector

logging.basicConfig(level=logging.CRITICAL)

CONFIG = {
    "VOICE_NAME": os.environ.get("JARVIS_VOICE", "Alex"),
    "AI_MODEL": "llama3.1:8b",
    "AI_URL": "http://localhost:11434/api/generate",
    "ENERGY_THRESHOLD": 300,
    "PAUSE_THRESHOLD": 0.8,
    "AMP_DURATION": 0.5,
}

DEV_TOOLS = {
    "Visual Studio Code": ["vscode", "code", "visual studio code"],
    "Terminal": ["terminal"],
    # other apps...
}

ALIAS_MAP = {app.lower(): app for app in DEV_TOOLS}
for app, aliases in DEV_TOOLS.items():
    for alias in aliases:
        ALIAS_MAP[alias.lower()] = app

SEARCH_ENGINES = {
    "google": "https://www.google.com/search?q=",
    "stackoverflow": "https://stackoverflow.com/search?q=",
    "github": "https://github.com/search?q=",
}

TIMER_RE = re.compile(r'(\d+)\s*(?:minute|min)\b', re.IGNORECASE)
JARVIS_VARIANTS = ("jarvis", "jarvish", "jarves")

class JarvisAssistant:
    def __init__(self):
        self.recognizer = sr.Recognizer()
        self.setup_recognizer()
        self.listening = True
        self.tts_proc = None
        self.stop_speaking_event = Event()
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.active_timers = {}
        self.timer_counter = 0
        self.conversation_history = []
        self.db = mysql.connector.connect(
            host="localhost", user="root", password="mysql", database="jarvis"
        )
        self.cursor = self.db.cursor(dictionary=True)
        Thread(target=self.reminder_loop, daemon=True).start()

    def setup_recognizer(self):
        self.recognizer.dynamic_energy_threshold = True
        self.recognizer.energy_threshold = CONFIG["ENERGY_THRESHOLD"]
        self.recognizer.pause_threshold = CONFIG["PAUSE_THRESHOLD"]
        self.recognizer.non_speaking_duration = 0.8

    def check_microphone(self):
        try:
            with sr.Microphone() as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=0.5)
            return True
        except Exception:
            return False

    def speak(self, text):
        self.stop_speaking()
        self.stop_speaking_event.clear()
        def _speak():
            try:
                self.tts_proc = subprocess.Popen(
                    ['say', '-v', CONFIG["VOICE_NAME"], text],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                self.tts_proc.wait()
            finally:
                self.tts_proc = None
        Thread(target=_speak, daemon=True).start()

    def stop_speaking(self):
        self.stop_speaking_event.set()
        if self.tts_proc and self.tts_proc.poll() is None:
            try:
                self.tts_proc.terminate()
                self.tts_proc.wait(timeout=0.1)
            except:
                try:
                    self.tts_proc.kill()
                except:
                    pass
            self.tts_proc = None

    def open_app(self, name):
        target = ALIAS_MAP.get(name.lower(), name)
        try:
            subprocess.Popen(['open', '-a', target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return target
        except:
            return None

    def web_search(self, query, engine="google"):
        url_base = SEARCH_ENGINES.get(engine)
        if not url_base:
            return False
        try:
            webbrowser.open(url_base + quote_plus(query))
            return True
        except:
            return False

    def ask_ai(self, prompt):
        try:
            hist = "\n".join(self.conversation_history[-6:])
            full = f"{hist}\nUser: {prompt}\nJarvis:"
            resp = requests.post(
                CONFIG["AI_URL"],
                json={"model":CONFIG["AI_MODEL"], "prompt":full, "stream":False, "options":{"temperature":0.3,"num_predict":100}},
                timeout=8
            )
            if resp.status_code != 200:
                return "AI unavailable"
            d = resp.json()
            rep = (d.get("response") or d.get("message") or "AI unavailable").strip()
            self.conversation_history.append(f"User: {prompt}")
            self.conversation_history.append(f"Jarvis: {rep}")
            return rep
        except:
            return "AI unavailable"

    def get_weather(self, city="ahmedabad"):
        try:
            r = requests.get(f"http://wttr.in/{city}?format=3", timeout=5)
            return r.text.strip() if r.status_code == 200 else "Weather unavailable"
        except:
            return "Weather unavailable"

    def set_timer(self, minutes, label="Timer"):
        self.timer_counter += 1
        tid = self.timer_counter
        def cb():
            self.speak(f"{label} finished")
            self.active_timers.pop(tid, None)
        t = Timer(minutes * 60, cb)
        self.active_timers[tid] = {'timer': t, 'label': label}
        t.start()
        return f"{label} set for {minutes} minutes"

    def add_reminder(self, message, remind_at):
        sql = "INSERT INTO reminders (message, remind_at) VALUES (%s, %s)"
        self.cursor.execute(sql, (message, remind_at))
        self.db.commit()
        return f"Reminder added: {message} at {remind_at}"

    def check_reminders(self):
        now = datetime.datetime.now().replace(second=0, microsecond=0)
        sql = "SELECT * FROM reminders WHERE remind_at = %s"
        self.cursor.execute(sql, (now,))
        rows = self.cursor.fetchall()
        for row in rows:
            self.speak(f"Reminder: {row['message']}")
            self.cursor.execute("DELETE FROM reminders WHERE id = %s", (row['id'],))
            self.db.commit()

    def reminder_loop(self):
        while True:
            self.check_reminders()
            time.sleep(60)

    def process_command(self, command):
        cmd = command.lower().strip()
        now = datetime.datetime.now()

        if "time" in cmd:
            self.speak(now.strftime("%I:%M %p")); return
        if "date" in cmd:
            self.speak(now.strftime("%A, %B %d")); return
        if "weather" in cmd:
            self.speak(self.get_weather()); return
        if any(g in cmd for g in ("hello","hi","hey")):
            self.speak("Hello"); return
        if cmd.startswith("open "):
            name = command[5:].strip()
            success = self.open_app(name)
            self.speak("Opening" if success else "App not found")
            return
        if cmd.startswith("search ") or cmd.startswith("google "):
            q = command[7:]
            self.speak("Searching" if self.web_search(q) else "Search failed"); return
        if cmd.startswith("timer"):
            m = TIMER_RE.search(cmd)
            if m:
                mins = int(m.group(1))
                self.speak(self.set_timer(mins))
            else:
                self.speak("Specify minutes")
            return

        if cmd.startswith("remind me"):
            try:
                parts = command.split(" at ")
                msg = parts[0].replace("remind me to", "").strip()
                tstr = parts[1].strip()
                remind_at = datetime.datetime.strptime(f"{now.date()} {tstr}", "%Y-%m-%d %H:%M")
                self.speak(self.add_reminder(msg, remind_at))
            except:
                self.speak("Could not set reminder")
            return

        self.speak(self.ask_ai(command))

    def _process_audio(self, audio):
        try:
            phrase = self.recognizer.recognize_google(audio, show_all=False)
            if not phrase or len(phrase) < 2:
                return
            phrase = phrase.lower().strip()
            for v in JARVIS_VARIANTS:
                if phrase.startswith(v):
                    cmd = phrase[len(v):].strip()
                    self.speak("Sure" if cmd else "Yes")
                    if cmd:
                        self.executor.submit(self.process_command, cmd)
                    return
                if f" {v} " in f" {phrase} ":
                    idx = phrase.split().index(v)
                    cmd = " ".join(phrase.split()[idx+1:])
                    self.speak("Sure" if cmd else "Yes")
                    if cmd:
                        self.executor.submit(self.process_command, cmd)
                    return
        except:
            pass

    def run(self):
        if not self.check_microphone():
            return
        signal.signal(signal.SIGINT, self.signal_handler)
        last_adj = time.time()
        while self.listening:
            try:
                with sr.Microphone() as src:
                    if time.time() - last_adj > 300:
                        self.recognizer.adjust_for_ambient_noise(src, duration=CONFIG["AMP_DURATION"])
                        last_adj = time.time()
                    audio = self.recognizer.listen(src, timeout=1, phrase_time_limit=8)
                    self.executor.submit(self._process_audio, audio)
            except sr.WaitTimeoutError:
                continue
            except:
                time.sleep(0.5)

    def signal_handler(self, signum, frame):
        self.listening = False
        self.stop_speaking()
        sys.exit(0)

if __name__ == "__main__":
    JarvisAssistant().run()
