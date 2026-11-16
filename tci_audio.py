"""AetherSDR TCI 2.1 audio and PTT for the managed Data2G host."""
from __future__ import annotations

import logging
import struct
import threading
import time

import numpy as np
from websocket import WebSocketException, WebSocketTimeoutException, create_connection

SAMPLE_RATE = 48_000
CHANNELS = 2
FRAME_BYTES = CHANNELS * 4
HEADER = struct.Struct("<16I")
FLOAT32, RX_AUDIO, TX_AUDIO, TX_CHRONO = 3, 1, 2, 3
REPLY_TIMEOUT = 2.0
CHRONO_TIMEOUT = 0.75
log = logging.getLogger(__name__)


class TciAudio:
    def __init__(self, port, receiver, stop):
        self.receiver, self.stop = receiver, stop
        self.condition = threading.Condition()
        self.rx = bytearray()
        self.tx = bytearray()
        self.rx_audio_seen = False
        self.tx_audio_sent = False
        self.tx_cycle_confirmed = False
        self.fault = None
        self.closing = False
        self.requested = False
        self.unkeying = False
        self.trx = None
        self.trx_updates = 0
        self.modulation = None
        self.last_chrono = None
        self.playing = False
        self.underrun = False
        self.ws = create_connection(f"ws://127.0.0.1:{port}", timeout=REPLY_TIMEOUT,
                                    suppress_origin=True, enable_multithread=True)
        try:
            self._wait_startup("ready")
            self.ws.send("audio_samplerate:48000;audio_stream_channels:2;audio_stream_sample_type:float32;"
                         f"audio_start:{self.receiver};")
            self._wait_startup(f"audio_start:{self.receiver}")
            self.ws.settimeout(0.1)
            self.reader = threading.Thread(target=self._receive, name="tci-audio", daemon=True)
            self.reader.start()
        except BaseException:
            self.ws.shutdown()
            raise

    def _wait_startup(self, expected):
        deadline = time.monotonic() + REPLY_TIMEOUT
        while time.monotonic() < deadline:
            self.ws.settimeout(max(0.01, deadline - time.monotonic()))
            message = self.ws.recv()
            if isinstance(message, str):
                if not message:
                    break
                fields = self._status(message)
                if expected in fields:
                    return
        raise RuntimeError(f"TCI server did not confirm {expected}")

    def _status(self, message):
        fields = {line.strip().lower() for line in message.split(";")}
        with self.condition:
            prefix = f"modulation:{self.receiver},"
            for field in fields:
                if field.startswith(prefix):
                    self.modulation = field[len(prefix):]
            for state in ("true", "false"):
                if f"trx:{self.receiver},{state}" in fields:
                    self.trx = state == "true"
                    self.trx_updates += 1
            self.condition.notify_all()
        return fields

    def _receive(self):
        try:
            while not self.closing:
                try:
                    message = self.ws.recv()
                except WebSocketTimeoutException:
                    message = None
                if message == "":
                    raise RuntimeError("TCI WebSocket closed")
                if isinstance(message, str):
                    self._status(message)
                elif isinstance(message, bytes):
                    self._audio_message(message)
                with self.condition:
                    if (self.requested and not self.unkeying and self.last_chrono is not None and
                            time.monotonic() - self.last_chrono > CHRONO_TIMEOUT):
                        raise RuntimeError("TCI transmit clock stalled")
        except (OSError, WebSocketException, RuntimeError) as exc:
            with self.condition:
                if not self.closing:
                    self.fault = exc
                    log.error("TCI audio stopped: %s", exc)
                    self.stop.set()
                self.condition.notify_all()

    def _audio_message(self, message):
        if len(message) < HEADER.size:
            return
        receiver, rate, fmt, _, _, samples, kind, channels, *_ = HEADER.unpack_from(message)
        if (receiver != self.receiver or rate != SAMPLE_RATE or fmt != FLOAT32 or
                channels != CHANNELS or samples <= 0 or samples % CHANNELS or samples > 8192):
            return
        byte_count = samples * 4
        if kind == RX_AUDIO and len(message) >= HEADER.size + byte_count:
            with self.condition:
                if not self.requested:
                    self.rx.extend(message[HEADER.size:HEADER.size + byte_count])
                    if not self.rx_audio_seen:
                        self.rx_audio_seen = True
                        log.info("TCI RX audio received")
                    excess = len(self.rx) - SAMPLE_RATE * FRAME_BYTES // 3
                    if excess > 0:
                        del self.rx[:excess]
        elif kind == TX_CHRONO:
            with self.condition:
                if not self.requested or self.unkeying:
                    return
                audio = bytes(self.tx[:byte_count])
                del self.tx[:byte_count]
                if self.playing and len(audio) < byte_count and not self.underrun:
                    log.warning("TCI TX audio underrun")
                    self.underrun = True
                self.ws.send_binary(
                    HEADER.pack(receiver, rate, FLOAT32, 0, 0, samples, TX_AUDIO, CHANNELS, *([0] * 8))
                    + audio.ljust(byte_count, b"\0"))
                if not self.tx_audio_sent and self.trx is True and any(audio):
                    self.tx_audio_sent = True
                self.last_chrono = time.monotonic()
                self.condition.notify_all()

    def ptt(self, on):
        with self.condition:
            if not on and not self.requested:
                self.tx.clear()
                return True
            if on and (self.fault or self.stop.is_set()):
                raise RuntimeError("TCI audio is unavailable")
            if on and self.trx:
                raise RuntimeError("TCI receiver is already transmitting")
            if on and self.modulation not in {"digu", "digl"}:
                raise RuntimeError("Set the selected TCI receiver to DIGU or DIGL before transmitting")
            updates = self.trx_updates
            if on:
                self.requested = True
                self.unkeying = False
                self.last_chrono = None
                self.tx_audio_sent = False
                self.rx.clear()
                self.tx.clear()
            else:
                self.unkeying = True
                self.playing = False
                self.tx.clear()
            try:
                self.ws.send(f"trx:{self.receiver},{'true,tci' if on else 'false'};")
                deadline = time.monotonic() + REPLY_TIMEOUT
                while not self.fault and time.monotonic() < deadline:
                    if on and self.last_chrono is not None and not self.stop.is_set():
                        return True
                    if not on and self.trx_updates > updates and self.trx is False:
                        self.requested = False
                        self.last_chrono = None
                        if self.tx_audio_sent and not self.tx_cycle_confirmed:
                            self.tx_cycle_confirmed = True
                            log.info("TCI TX audio sent and PTT cycle confirmed")
                        return True
                    if on and self.stop.is_set():
                        break
                    self.condition.wait(min(0.05, max(0, deadline - time.monotonic())))
            except (OSError, WebSocketException) as exc:
                log.warning("TCI PTT command failed: %s", exc)
            if on:
                if self.stop.is_set() and self.fault is None:
                    return False
                log.error("PTT on failed through TCI")
                raise RuntimeError("AetherSDR did not provide a TCI transmit clock")
            log.error("PTT off failed through TCI")
            self.stop.set()
            return False

    def release(self):
        self.ptt(False)

    def close(self):
        with self.condition:
            self.closing = True
            self.condition.notify_all()
        # An abrupt socket shutdown also revokes this client's PTT ownership.
        self.ws.abort()
        self.reader.join(timeout=0.5)
        self.ws.shutdown()

    @staticmethod
    def get_default_input_device_info():
        return {"maxInputChannels": CHANNELS}

    @staticmethod
    def get_default_output_device_info():
        return {"maxOutputChannels": CHANNELS}


def use_tci_audio(host, port, receiver, on_capture=None):
    """Reuse the Data2G engine, KISS server, gain, and transmit lifecycle."""
    original_serve = host.serve

    def serve(arguments, _pa, stop=None):
        stop = stop or threading.Event()
        try:
            audio = TciAudio(port, receiver, stop)
        except (OSError, WebSocketException, RuntimeError) as exc:
            raise SystemExit(f"TCI connection failed on port {port}: {exc}") from exc

        class Capture:
            def __init__(self, _audio, _channels, _rate, _device):
                self.next_block = time.monotonic()

            def read(self, frames, stop):
                period = frames / SAMPLE_RATE
                self.next_block = max(self.next_block + period, time.monotonic())
                if stop.wait(max(0, self.next_block - time.monotonic())):
                    return None
                size = frames * FRAME_BYTES
                with audio.condition:
                    block = bytes(audio.rx[:size])
                    del audio.rx[:size]
                block = block.ljust(size, b"\0")
                if on_capture is not None:
                    on_capture(block, CHANNELS, SAMPLE_RATE)
                return np.frombuffer(block, dtype="<f4")[::CHANNELS].astype(np.float64)

            def close(self):
                pass

        class Player:
            def __init__(self, _audio, _channels, _rate, _device, lead):
                self.lead = bytes(int(lead * SAMPLE_RATE) * FRAME_BYTES)

            def start(self):
                with audio.condition:
                    audio.tx.extend(self.lead)
                    audio.playing = True
                    audio.underrun = False

            def write(self, samples):
                with audio.condition:
                    if not stop.is_set():
                        if len(audio.tx) >= SAMPLE_RATE * FRAME_BYTES:
                            raise RuntimeError("TCI TX audio is more than one second behind")
                        audio.tx.extend(np.repeat(samples.astype("<f4"), CHANNELS).tobytes())

            def drain(self):
                end = time.monotonic() + REPLY_TIMEOUT
                with audio.condition:
                    audio.playing = False
                    while audio.tx and not stop.is_set():
                        remaining = end - time.monotonic()
                        if remaining <= 0:
                            raise RuntimeError("Timed out draining TCI transmit audio")
                        audio.condition.wait(min(0.05, remaining))
                stop.wait(0.05)

            def close(self):
                pass

        # The managed host runs one serve call in an isolated subprocess.
        host.Capture, host.Player = Capture, Player
        host.Rigctld = lambda *_: audio
        arguments.sample_rate = SAMPLE_RATE
        arguments.input_device = arguments.output_device = None
        try:
            log.info("TCI audio connected on port %d, receiver %d", port, receiver)
            original_serve(arguments, audio, stop)
        finally:
            audio.close()

    host.serve = serve
