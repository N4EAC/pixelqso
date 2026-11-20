import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication
import app


class FilePlaybackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def test_stop_rejects_late_audio_and_decode_events(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(
                os.environ, {"PIXELQSO_DATA_DIR": folder}), patch("app.QTimer.singleShot"):
            window = app.MainWindow()
            try:
                window.file_player = Mock()
                token = window.file_playback_token
                window.decode_jobs = 1
                window.file_playback_active = True
                window.stop_file_playback()
                window._start_file_playback(
                    (Path(folder, "old.wav"), token, (np.ones(4800), 48000)))
                with patch.object(window, "decode_finished") as decoded:
                    window._file_decode_finished((token, ("old.wav", {}, None)))
                window.file_player.play.assert_not_called()
                decoded.assert_not_called()
                self.assertEqual(window.decode_jobs, 0)
                self.assertFalse(window.file_stop_button.isEnabled())
            finally:
                window.close()

    def test_completion_keeps_pending_decode_and_waterfall_tracks_position(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(
                os.environ, {"PIXELQSO_DATA_DIR": folder}), patch("app.QTimer.singleShot"):
            window = app.MainWindow()
            try:
                window.file_player = Mock()
                window.file_player.position.return_value = 250
                window.file_player.source.return_value = QUrl.fromLocalFile("/tmp/test.wav")
                window.file_playback_active = True
                window.file_audio_loaded = True
                window.file_audio_rate = 48000
                window.file_audio_samples = np.ones(48000, dtype=np.float32) * .2
                token = window.file_playback_token
                with patch.object(window.rx_waterfall, "feed_pcm16") as feed:
                    window._update_file_playback()
                self.assertEqual(sum(len(call.args[0]) for call in feed.call_args_list), 24000)
                self.assertIn("0.2 / 1.0 s", window.file_playback_status.text())
                window._file_media_status(app.QMediaPlayer.MediaStatus.EndOfMedia)
                self.assertEqual(window.file_playback_token, token)
                result = ("test.wav", {}, None)
                with patch.object(window, "decode_finished") as decoded:
                    window._file_decode_finished((token, result))
                decoded.assert_called_once_with(result)
                self.assertEqual(window.file_playback_status.text(), "Audio file playback complete")
            finally:
                window.close()
