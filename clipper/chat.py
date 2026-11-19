import asyncio, json, logging, threading, time
import requests, websockets
from .config import env

log = logging.getLogger("chat")
KICK_KEY = env("KICK_PUSHER_KEY", "32cbd69e4b950bf97679")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 Chrome/130 Safari/537.36",
      "Accept": "application/json"}


async def _twitch(chan, out, stop):
    async with websockets.connect("wss://irc-ws.chat.twitch.tv:443", ping_interval=60) as ws:
        await ws.send("PASS SCHMOOPIIE")
        await ws.send("NICK justinfan%d" % (int(time.time()) % 99999))
        await ws.send(f"JOIN #{chan.lower()}")
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), 5)
            except asyncio.TimeoutError:
                continue
            for line in raw.split("\r\n"):
                if line.startswith("PING"):
                    await ws.send("PONG :tmi.twitch.tv")
                elif " PRIVMSG " in line:
                    out(line.split(" :", 1)[-1] if line.startswith(":") else line.split("PRIVMSG", 1)[1].split(" :", 1)[-1])


async def _kick(chan, out, stop):
    room = requests.get(f"https://kick.com/api/v2/channels/{chan}", headers=UA, timeout=20).json()["chatroom"]["id"]
    url = f"wss://ws-us2.pusher.com/app/{KICK_KEY}?protocol=7&client=js&version=8.4.0&flash=false"
    async with websockets.connect(url, ping_interval=60) as ws:
        await ws.send(json.dumps({"event": "pusher:subscribe", "data": {"auth": "", "channel": f"chatrooms.{room}.v2"}}))
        while not stop.is_set():
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), 5))
            except asyncio.TimeoutError:
                continue
            if m.get("event") == "pusher:ping":
                await ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
            elif m.get("event", "").endswith("ChatMessageEvent"):
                out(json.loads(m["data"]).get("content", ""))


class ChatLogger:
    def __init__(self, streamer, path):
        self.s, self.path, self.stop = streamer, path, threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True, name=f"chat-{streamer['name']}")

    def start(self):
        if self.s["platform"] in ("twitch", "kick"):
            self.t.start()
        return self

    def _run(self):
        fn = _twitch if self.s["platform"] == "twitch" else _kick
        with open(self.path, "a", encoding="utf-8") as f:
            def out(msg):
                f.write(json.dumps({"t": time.time(), "m": msg}, ensure_ascii=False) + "\n")
                f.flush()
            while not self.stop.is_set():
                try:
                    asyncio.run(fn(self.s["channel"], out, self.stop))
                except Exception as e:
                    log.warning("%s chat: %s", self.s["name"], e)
                    self.stop.wait(15)
