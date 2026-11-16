from queue import Empty, Queue
from threading import Event
from unittest.mock import patch

import numpy as np
import pytest
from websocket import WebSocketTimeoutException

from tci_audio import CHANNELS, FLOAT32, HEADER, RX_AUDIO, SAMPLE_RATE, TX_AUDIO, TX_CHRONO, TciAudio


def frame(receiver, kind, samples, payload=b""):
    return HEADER.pack(receiver, SAMPLE_RATE, FLOAT32, 0, 0, samples, kind, CHANNELS, *([0] * 8)) + payload


class TciSocket:
    def __init__(self, mode="digu"):
        self.incoming = Queue()
        self.incoming.put(f"modulation:0,{mode};trx:0,false;ready;")
        self.commands = []
        self.audio = Queue()
        self.timeout = 0.1

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv(self):
        try:
            return self.incoming.get(timeout=self.timeout)
        except Empty as error:
            raise WebSocketTimeoutException() from error

    def send(self, command):
        self.commands.append(command)
        if "audio_start:0;" in command:
            self.incoming.put("audio_start:0;")
        elif command == "trx:0,true,tci;":
            self.incoming.put("trx:0,true;")
            self.incoming.put(frame(0, TX_CHRONO, 4))
        elif command == "trx:0,false;":
            self.incoming.put("trx:0,false;")

    def send_binary(self, data):
        self.audio.put(data)

    def abort(self):
        self.incoming.put("")

    def shutdown(self):
        pass


def test_tci_waits_for_chrono_and_releases_its_ptt():
    ws = TciSocket()
    with patch("tci_audio.create_connection", return_value=ws) as connect:
        client = TciAudio(60000, 0, Event())
    try:
        assert connect.call_args.args[0] == "ws://127.0.0.1:60000"
        assert not client.requested
        assert client.ptt(True)
        silent_reply = ws.audio.get(timeout=1)
        assert silent_reply == frame(0, TX_AUDIO, 4, bytes(16))
        pcm = np.array([.15, .15, -.15, -.15], dtype="<f4").tobytes()
        with client.condition:
            client.tx.extend(pcm)
        ws.incoming.put(frame(0, TX_CHRONO, 4))
        assert ws.audio.get(timeout=1) == frame(0, TX_AUDIO, 4, pcm)
        client.release()
        assert not client.requested
        assert ws.commands[-2:] == ["trx:0,true,tci;", "trx:0,false;"]
    finally:
        client.close()


def test_tci_filters_receivers_and_refuses_voice_mode_ptt():
    ws = TciSocket(mode="usb")
    with patch("tci_audio.create_connection", return_value=ws):
        client = TciAudio(60000, 0, Event())
    try:
        pcm = np.array([.2, .2, -.2, -.2], dtype="<f4").tobytes()
        client._audio_message(frame(1, RX_AUDIO, 4, pcm))
        assert not client.rx
        client._audio_message(frame(0, RX_AUDIO, 4, pcm))
        assert client.rx == pcm
        with pytest.raises(RuntimeError, match="DIGU or DIGL"):
            client.ptt(True)
        client.release()
        assert len(ws.commands) == 1
    finally:
        client.close()
