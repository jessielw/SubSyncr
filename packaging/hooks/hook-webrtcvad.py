"""PyInstaller compatibility hook for the maintained webrtcvad-wheels fork."""

from PyInstaller.utils.hooks import copy_metadata

datas = copy_metadata("webrtcvad-wheels")
