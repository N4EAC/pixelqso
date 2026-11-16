"""Launch Data2G with TX-advertised burst lengths enabled on receive.

Older Data2G engines cap receive admission at 16 seconds, even when a legal
256-byte KISS frame needs longer. Keep the compatibility override for those
hosts. Upstream PR #65 supplies receive_accept itself and needs no override.
The wrapper also supplies PixelQSO's frame-associated receive-SNR metadata.
"""
from __future__ import annotations

import argparse
import sys
import hashlib
import logging
import math
import os
import socket
import struct
from collections import defaultdict, deque
from types import SimpleNamespace

from snr import KISS_RX_SNR, encode_rx_snr


def limit_aether_dax_tx_buffer() -> None:
    """Keep macOS DAX playback batches below the bridge's backlog limit."""
    from data2g import host

    class DaxPlaybackPlayer(host.Player):
        def __init__(self, pa, nout, rate, device, lead_s):
            output = (pa.get_default_output_device_info() if device is None
                      else pa.get_device_info_by_index(device))
            if output.get("name") == "AetherSDR TX":
                open_stream = pa.open

                def open_dax_output(*, frames_per_buffer, **kwargs):
                    frames = min(frames_per_buffer, 256)
                    logging.getLogger(__name__).info(
                        "Opening AetherSDR TX at %d Hz with a %d-frame buffer", rate, frames)
                    return open_stream(frames_per_buffer=frames, **kwargs)

                pa = SimpleNamespace(open=open_dax_output)
            super().__init__(pa, nout, rate, device, lead_s)

    host.Player = DaxPlaybackPlayer


def waterfall_audio_datagram(data, channels: int, sample_rate: int) -> bytes:
    """Encode the host's left-channel float capture for the UI waterfall."""
    import numpy as np
    samples = np.frombuffer(data, dtype=np.float32)[::max(1, int(channels))]
    return struct.pack("!I", int(sample_rate)) + samples.astype("<f4", copy=False).tobytes()


def waterfall_sender(port: int):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(data, channels, rate):
        try:
            # Keep each packet below macOS's UDP datagram size limit.
            frame_bytes = channels * 4
            for offset in range(0, len(data), 1024 * frame_bytes):
                packet = waterfall_audio_datagram(
                    data[offset:offset + 1024 * frame_bytes], channels, rate)
                sender.sendto(packet, ("127.0.0.1", port))
        except (OSError, TypeError, ValueError):
            pass  # Waterfall is optional and must never interrupt radio RX.

    return send


def enable_waterfall_audio(port: int) -> None:
    """Mirror local-host capture blocks to PixelQSO's read-only waterfall."""
    from data2g.host import Capture

    send = waterfall_sender(port)
    original_take = Capture._take

    def take(self, data, nframes, timing, status):
        result = original_take(self, data, nframes, timing, status)
        send(data, self.nin, self.rate)
        return result

    Capture._take = take


def enable_advertised_receive_lengths() -> None:
    """Retain long-frame reception on old hosts; respect PR #65 on new ones."""
    from data2g import modem
    from data2g.arq import engine

    if callable(getattr(engine, "receive_accept", None)):
        return

    original = modem.Accept.of.__func__
    if getattr(original, "_pixelqso_full_receive", False):
        return

    def of(cls, names=None, max_secs=None, min_score=0.0):
        # Keep explicit submode restrictions, and all score checks. Drop only
        # the extra seconds cap; Accept.of still bounds each mode by the count
        # its protected header can represent.
        if names is None:
            max_secs = None
        return original(cls, names, max_secs, min_score)

    of._pixelqso_full_receive = True
    modem.Accept.of = classmethod(of)


def enable_receive_snr():
    """Send frame-associated PHY measurements ahead of data on the same KISS socket."""
    from data2g.kisslink import KissLink
    from data2g.tnc import KissServer, kiss_encode
    from data2g.arq import phy
    from data2g.config import SNR_REF_BW_HZ

    original_receive = KissLink.on_burst
    original_broadcast = KissServer.broadcast
    if getattr(original_receive, "_pixelqso_receive_snr", False):
        return
    pending = defaultdict(deque)

    def on_burst(self, received, rx=None):
        old_events = len(getattr(self, "events", []))
        frames = original_receive(self, received, rx)
        heard = [event.split() for event in getattr(self, "events", [])[old_events:]
                 if event.startswith("BCAST ") and len(event.split()) >= 3 and event.split()[2] == "HEARD"]
        if frames or heard:
            try:
                value = float(phy.measure(received)["snr_est"])
                if math.isfinite(value):
                    if sum(map(len, pending.values())) + len(frames or []) > 256:
                        pending.clear()
                    for port, frame in frames or []:
                        digest = hashlib.sha256(frame).hexdigest()
                        pending[(port, digest)].append(encode_rx_snr(digest, value, SNR_REF_BW_HZ))
                    if not frames:
                        mode = getattr(received.get("spec"), "name", "unknown")
                        count = int(received.get("n_cw", 0))
                        for event in heard:
                            call = event[3] if len(event) > 3 else "-"
                            self.events.append(f"BCAST {event[1]} RXMETADATA {value} "
                                               f"{int(SNR_REF_BW_HZ)} {call} {mode} {count}")
            except (KeyError, TypeError, ValueError, OverflowError):
                pass  # Measurement failure must not discard a decoded frame.
        return frames

    def broadcast(self, data, port=0):
        key = (port, hashlib.sha256(data).hexdigest())
        records = pending.get(key)
        if not records:
            return original_broadcast(self, data, port)
        metadata = records.popleft()
        if not records:
            del pending[key]
        wire = kiss_encode(metadata, port, KISS_RX_SNR) + kiss_encode(data, port)
        with self.lock:
            for client in list(self.clients):
                try:
                    client.sendall(wire)
                except OSError:
                    self.clients.discard(client)

    on_burst._pixelqso_receive_snr = True
    KissLink.on_burst = on_burst
    KissServer.broadcast = broadcast


def main() -> int:
    from data2g import host

    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--pixelqso-waterfall-port", type=int)
    parser.add_argument("--pixelqso-tci-port", type=int)
    parser.add_argument("--pixelqso-tci-receiver", type=int, default=0)
    options, remaining = parser.parse_known_args(sys.argv[1:])
    waterfall_port = options.pixelqso_waterfall_port
    enable_advertised_receive_lengths()
    enable_receive_snr()
    if options.pixelqso_tci_port is not None:
        from tci_audio import use_tci_audio

        if not 1 <= options.pixelqso_tci_port <= 65535 or options.pixelqso_tci_receiver < 0:
            parser.error("invalid TCI port or receiver")
        use_tci_audio(host, options.pixelqso_tci_port, options.pixelqso_tci_receiver,
                      waterfall_sender(waterfall_port) if waterfall_port else None)
    else:
        if waterfall_port:
            enable_waterfall_audio(waterfall_port)
        if sys.platform == "darwin":
            limit_aether_dax_tx_buffer()

    sys.argv = ["data2g-host", *remaining]
    host.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
