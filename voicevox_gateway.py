import io
import queue
import random
import threading
from collections.abc import Iterable

import requests
import sounddevice as sd
import soundfile as sf

LLM_URL = "http://127.0.0.1:11434/api/generate"   # Ollamaの場合
VOICEVOX = "http://127.0.0.1:50021"
SPEAKER = 1  # VOICEVOX speaker/style ID used by dashboard announcements
ANNOUNCEMENT_SPEAKERS = {
    1: "Default (ID 1)",
    47: "ナースロボ＿タイプＴ - ノーマル",
    48: "ナースロボ＿タイプＴ - 楽々",
    49: "ナースロボ＿タイプＴ - 恐怖",
    50: "ナースロボ＿タイプＴ - 内緒話",
    14: "冥鳴ひまり",
    8: "春日部つむぎ",
    118: "夜語トバリ - ノーマル",
    119: "夜語トバリ - 明るい",
    120: "夜語トバリ - 哀しみ",
    121: "夜語トバリ - 呆れ",
    122: "暁記ミタマ - ノーマル",
    123: "暁記ミタマ - 怒り",
    124: "暁記ミタマ - 哀しみ",
    125: "暁記ミタマ - ささやき",
}
NAVY_DIGITS = ("マル", "ヒト", "フタ", "サン", "ヨン", "ゴー", "ロク", "ナナ", "ハチ", "キュウ")
_speech_lock = threading.Lock()
_speech_state_lock = threading.Lock()
_speech_listeners_lock = threading.Lock()
_speech_listeners: set[queue.Queue[bool]] = set()
_active_speech_count = 0


def _publish_speech_state(speaking: bool) -> None:
    with _speech_listeners_lock:
        for listener in _speech_listeners:
            if listener.full():
                try:
                    listener.get_nowait()
                except queue.Empty:
                    pass
            try:
                listener.put_nowait(speaking)
            except queue.Full:
                pass


def subscribe_speech_events() -> queue.Queue[bool]:
    listener: queue.Queue[bool] = queue.Queue(maxsize=1)
    with _speech_state_lock:
        with _speech_listeners_lock:
            _speech_listeners.add(listener)
            listener.put_nowait(_active_speech_count > 0)
    return listener


def unsubscribe_speech_events(listener: queue.Queue[bool]) -> None:
    with _speech_listeners_lock:
        _speech_listeners.discard(listener)


def is_speaking() -> bool:
    with _speech_state_lock:
        return _active_speech_count > 0


def generate(prompt: str) -> str:
    r = requests.post(LLM_URL, json={
        "model": "your-model",
        "prompt": prompt,
        "stream": False,
    })
    return r.json()["response"]


def navy_time_reading(time_value: str) -> str:
    """Convert a validated HH:MM time into Japanese navy-style digit readings."""
    hour, minute = time_value.split(":")
    digits = f"{hour}{minute}"
    return "".join(NAVY_DIGITS[int(digit)] for digit in digits)


def _speak_audio(text: str, speaker: int) -> None:
    query_response = requests.post(
        f"{VOICEVOX}/audio_query",
        params={
            "text": text,
            "speaker": speaker
        },
        timeout=10
    )
    query_response.raise_for_status()
    query = query_response.json()

    response = requests.post(
        f"{VOICEVOX}/synthesis",
        params={
            "speaker": speaker
        },
        json=query,
        timeout=30
    )
    response.raise_for_status()

    data, sr = sf.read(io.BytesIO(response.content))
    sd.play(data, sr)
    sd.wait()


def speak_sequence(texts: Iterable[str], speaker: int = SPEAKER) -> None:
    global _active_speech_count

    text_iterator = iter(texts)
    try:
        first_text = next(text_iterator)
    except StopIteration:
        return

    with _speech_state_lock:
        _active_speech_count += 1
        _publish_speech_state(True)
    try:
        with _speech_lock:
            _speak_audio(first_text, speaker)
            for text in text_iterator:
                _speak_audio(text, speaker)
    finally:
        with _speech_state_lock:
            _active_speech_count -= 1
            _publish_speech_state(_active_speech_count > 0)


def speak(text: str, speaker: int = SPEAKER) -> None:
    speak_sequence([text], speaker)




if __name__ == "__main__":
    robo_style = random.randint(47, 50) # ナースロボ＿タイプＴ ノーマル47 楽々48 恐怖49 内緒話50
    # speak(generate("今日の天気について一言お願いします"))
    speak("無印良品、クレカ「MUJI Card」カード番号全桁、約109万件漏えいか　不正アクセスで続報、2021年7月までに入会した会員の情報で、カード番号の全桁、MUJI Card ID、生年月日が含まれる。", 
          speaker=robo_style)
    robo_style = random.randint(47, 50) # ナースロボ＿タイプＴ ノーマル47 楽々48 恐怖49 内緒話50
    # 冥鳴ひまり 14
    # 春日部つむぎ 8
    # 夜語トバリ ノーマル118 明るい119 哀しみ120 呆れ121